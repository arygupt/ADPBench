// 002_gemv : y = A @ x  for A (ROWS x COLS) int8 row-major, x (COLS) int8
//
// Streams (independent valid/ready, LANES elements per beat):
//   in_a_flat : ROWS*COLS elements, row-major   -> ROWS*COLS/LANES beats
//   in_x_flat : COLS elements                   -> COLS/LANES beats
//   out_c     : ROWS int32 words, row order
//
// Structure:
//   * x is captured into a COLS-byte register file, one LANES-wide group per
//     beat (group g holds columns [g*LANES, g*LANES+LANES) ).
//   * A beats are consumed row by row; a beat supplies one column group of the
//     current row.  LANES signed 8x8 products are summed by a balanced adder
//     tree into a per-row accumulator.  Internal widths are the exact minimum
//     for a signed reduction; the row result is sign-extended to ACC_W only at
//     the output.
//   * When a row's last group is consumed the row result is pushed into a
//     small FIFO.  A full FIFO stalls the A stream.
//   * An A beat for column group g is only accepted once that group's x beat
//     has arrived in the current transaction, so the two streams pace each
//     other and x may lag A arbitrarily without any data loss.
//   * Per-transaction state (the x-valid counter) is re-armed on the last A
//     beat of a transaction; the row/column/output counters wrap naturally, so
//     back-to-back transactions work with no reset in between.

module dut #(
    parameter ROWS   = 16,
    parameter COLS   = 64,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam integer BEATS_X = COLS / LANES;          // x beats per transaction
    localparam integer BPR     = COLS / LANES;          // A beats per row
    localparam integer FIFO_D  = 2;                     // output skid depth

    localparam integer ROWW = $clog2(ROWS);             // row counter width
    localparam integer COLW = $clog2(BPR);              // column-group counter
    localparam integer XCW  = $clog2(BEATS_X + 1);      // x beat counter
    localparam integer PW   = $clog2(FIFO_D);           // FIFO address width

    // exact minimum signed widths of the reduction tree
    localparam integer W_L1 = 2*DATA_W + 1;             // sum of 2 products
    localparam integer W_L2 = 2*DATA_W + 2;             // sum of 4
    localparam integer W_L3 = 2*DATA_W + 3;             // sum of 8
    localparam integer W_PS = 2*DATA_W + 4;             // sum of LANES
    localparam integer W_AC = 2*DATA_W + $clog2(COLS);  // full row sum

    // ---------------------------------------------------------------- state
    reg [ROWW-1:0] row_cnt;      // current row of A
    reg [COLW-1:0] col_cnt;      // current column group within the row
    reg [XCW-1:0]  x_cnt;        // x beats accepted in this transaction
    reg [COLS*DATA_W-1:0] x_buf; // captured x values
    reg signed [W_AC-1:0] acc;   // running row accumulator

    reg [W_AC-1:0] fifo_mem [0:FIFO_D-1];
    reg [PW:0] wptr, rptr;

    wire [PW-1:0] widx = wptr[PW-1:0];
    wire [PW-1:0] ridx = rptr[PW-1:0];

    // ------------------------------------------------------- input handshake
    wire x_accept = in_x_flat_valid && in_x_flat_ready;
    assign in_x_flat_ready = rst_n && (x_cnt < BEATS_X);

    wire fifo_empty = (wptr == rptr);
    wire fifo_full  = (wptr[PW] != rptr[PW]) && (widx == ridx);
    wire producing  = (col_cnt == (BPR-1));

    // the x group for the current column is present, and (if this beat would
    // produce an output word) the output FIFO has room for it
    assign in_a_flat_ready = rst_n && (x_cnt > col_cnt) && !(producing && fifo_full);

    wire a_accept = in_a_flat_valid && in_a_flat_ready;
    wire push     = a_accept && producing;
    wire pop      = out_valid && out_ready;

    // ------------------------------------------------------------- x readout
    wire [LANES*DATA_W-1:0] x_lane = x_buf[col_cnt*(LANES*DATA_W) +: LANES*DATA_W];

    // ------------------------------------------------------------- datapath
    wire signed [W_L1-1:0] l1 [0:LANES/2-1];
    wire signed [W_L2-1:0] l2 [0:LANES/4-1];
    wire signed [W_L3-1:0] l3 [0:LANES/8-1];
    wire signed [W_PS-1:0] psum;

    genvar gi;
    generate
        for (gi = 0; gi < LANES/2; gi = gi + 1) begin : g_l1
            assign l1[gi] =
                  ($signed(in_a_flat[(2*gi  )*DATA_W +: DATA_W]) *
                   $signed(x_lane  [(2*gi  )*DATA_W +: DATA_W]))
                + ($signed(in_a_flat[(2*gi+1)*DATA_W +: DATA_W]) *
                   $signed(x_lane  [(2*gi+1)*DATA_W +: DATA_W]));
        end
        for (gi = 0; gi < LANES/4; gi = gi + 1) begin : g_l2
            assign l2[gi] = l1[2*gi] + l1[2*gi+1];
        end
        for (gi = 0; gi < LANES/8; gi = gi + 1) begin : g_l3
            assign l3[gi] = l2[2*gi] + l2[2*gi+1];
        end
        assign psum = l3[0] + l3[1];
    endgenerate

    wire signed [W_AC-1:0] acc_next =
        (col_cnt == {COLW{1'b0}}) ? $signed(psum) : (acc + $signed(psum));

    // ------------------------------------------------------------- output
    wire signed [W_AC-1:0] out_int = fifo_mem[ridx];

    assign out_valid = !fifo_empty;
    assign out_c     = {{(ACC_W-W_AC){out_int[W_AC-1]}}, out_int};

    // ------------------------------------------------------------- sequential
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            row_cnt <= {ROWW{1'b0}};
            col_cnt <= {COLW{1'b0}};
            x_cnt   <= {XCW{1'b0}};
            x_buf   <= {(COLS*DATA_W){1'b0}};
            acc     <= {W_AC{1'b0}};
            wptr    <= {(PW+1){1'b0}};
            rptr    <= {(PW+1){1'b0}};
        end else begin
            // capture one x beat into its group slot
            if (x_accept) begin
                case (x_cnt)
                    3'd0: x_buf[0*(LANES*DATA_W) +: LANES*DATA_W] <= in_x_flat;
                    3'd1: x_buf[1*(LANES*DATA_W) +: LANES*DATA_W] <= in_x_flat;
                    3'd2: x_buf[2*(LANES*DATA_W) +: LANES*DATA_W] <= in_x_flat;
                    3'd3: x_buf[3*(LANES*DATA_W) +: LANES*DATA_W] <= in_x_flat;
                    default: ;
                endcase
                x_cnt <= x_cnt + 3'd1;
            end

            // consume one A beat
            if (a_accept) begin
                acc <= acc_next;
                if (producing) begin
                    col_cnt <= {COLW{1'b0}};
                    if (row_cnt == (ROWS-1)) begin
                        row_cnt <= {ROWW{1'b0}};
                        // end of transaction: re-arm x capture for the next one
                        x_cnt   <= {XCW{1'b0}};
                    end else begin
                        row_cnt <= row_cnt + 1'b1;
                    end
                end else begin
                    col_cnt <= col_cnt + 1'b1;
                end
            end

            // push the finished row into the output FIFO
            if (push) begin
                fifo_mem[widx] <= acc_next;
                wptr <= wptr + 1'b1;
            end
            if (pop) begin
                rptr <= rptr + 1'b1;
            end
        end
    end

endmodule
