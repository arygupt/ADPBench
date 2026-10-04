// 002_gemv : y = A @ x
//   A : ROWS x COLS, int8, row-major (A[i][j] at i*COLS+j)
//   x : COLS, int8
//   y : ROWS, int32 exact
//
// Streaming structure (LANES = 16 elements per beat):
//   * in_x_flat carries COLS/LANES beats which are captured into X_BEATS
//     register banks.  An A beat is accepted as soon as the x chunk it
//     needs is already stored, so the two input streams need no alignment
//     and A starts one cycle after the first x beat.
//   * in_a_flat carries (ROWS*COLS)/LANES beats; beat k belongs to row
//     k>>log2(ROW_BEATS) and column chunk k % ROW_BEATS, so the x chunk
//     needed by that beat is selected purely from the beat counter.
//   * Each lane forms the exact signed 8x8 product of its A element and
//     the matching x element.  The LANES products are summed by a
//     balanced, minimal-width adder tree and accumulated into the row
//     accumulator.  On the last beat of a row the completed 32-bit row
//     word is registered as the output.
//   * A transaction ends when its final output word is accepted; all
//     per-transaction state (counters, accumulator) is cleared then, so
//     back-to-back transactions without reset work.
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

    localparam A_BEATS   = (ROWS*COLS)/LANES;         // 64
    localparam X_BEATS   = COLS/LANES;                // 4
    localparam ROW_BEATS = COLS/LANES;                // 4 beats per row
    localparam CLOG      = $clog2(ROW_BEATS);         // 2
    localparam XLOG      = $clog2(X_BEATS);           // 2
    localparam A_CNT_W   = $clog2(A_BEATS+1);         // 7
    localparam X_CNT_W   = $clog2(X_BEATS+1);         // 3
    localparam CHUNK_W   = LANES*DATA_W;              // 128
    localparam SUM_W     = 2*DATA_W + $clog2(LANES);  // 20

    // ---------------------------------------------------------------
    // per-transaction state
    // ---------------------------------------------------------------
    reg  [A_CNT_W-1:0]      a_cnt;      // A beats consumed (0..A_BEATS)
    reg  [X_CNT_W-1:0]      x_cnt;      // x beats stored   (0..X_BEATS)
    reg  signed [ACC_W-1:0] acc;        // running row accumulator
    reg  signed [ACC_W-1:0] out_c_r;    // output word register
    reg                     out_valid_r;

    // x sample storage: X_BEATS banks of LANES bytes
    reg [CHUNK_W-1:0] xbank [0:X_BEATS-1];

    wire [XLOG-1:0] wsel = x_cnt[XLOG-1:0];        // write bank
    wire [CLOG-1:0] csel = a_cnt[CLOG-1:0];        // chunk of the current row
    wire [CHUNK_W-1:0] x_beat = xbank[csel];       // x chunk for the A beat

    wire x_done    = (x_cnt == X_BEATS);
    wire a_done    = (a_cnt == A_BEATS);
    wire out_stall = out_valid_r & ~out_ready;
    wire chunk_ok  = (x_cnt > {{(X_CNT_W-CLOG){1'b0}}, csel});

    assign in_x_flat_ready = ~x_done;
    assign in_a_flat_ready = chunk_ok & ~a_done & ~out_stall;

    wire x_acc = in_x_flat_valid & in_x_flat_ready;
    wire a_acc = in_a_flat_valid & in_a_flat_ready;

    wire last_beat = a_acc & (csel == {CLOG{1'b1}});   // completes a row
    wire txn_end   = out_valid_r & out_ready & a_done; // final output taken

    // ---------------------------------------------------------------
    // LANES exact signed products
    // ---------------------------------------------------------------
    wire signed [15:0] lanep [0:LANES-1];

    genvar gj;
    generate
        for (gj = 0; gj < LANES; gj = gj + 1) begin : G_LANE
            wire signed [DATA_W-1:0] al = in_a_flat[gj*DATA_W +: DATA_W];
            wire signed [DATA_W-1:0] xl = x_beat[gj*DATA_W +: DATA_W];
            assign lanep[gj] = al * xl;
        end
    endgenerate

    // ---------------------------------------------------------------
    // balanced minimal-width reduction of the LANES products
    // ---------------------------------------------------------------
    wire signed [16:0] t1 [0:7];
    wire signed [17:0] t2 [0:3];
    wire signed [18:0] t3 [0:1];
    wire signed [19:0] beat_sum;

    genvar gi;
    generate
        for (gi = 0; gi < 8; gi = gi + 1) begin : G_T1
            assign t1[gi] = {{1{lanep[2*gi][15]}},   lanep[2*gi]}
                          + {{1{lanep[2*gi+1][15]}}, lanep[2*gi+1]};
        end
        for (gi = 0; gi < 4; gi = gi + 1) begin : G_T2
            assign t2[gi] = {{1{t1[2*gi][16]}},   t1[2*gi]}
                          + {{1{t1[2*gi+1][16]}}, t1[2*gi+1]};
        end
        for (gi = 0; gi < 2; gi = gi + 1) begin : G_T3
            assign t3[gi] = {{1{t2[2*gi][17]}},   t2[2*gi]}
                          + {{1{t2[2*gi+1][17]}}, t2[2*gi+1]};
        end
        assign beat_sum = {{1{t3[0][18]}}, t3[0]} + {{1{t3[1][18]}}, t3[1]};
    endgenerate

    wire signed [ACC_W-1:0] beat_ext =
        $signed({{(ACC_W-SUM_W){beat_sum[SUM_W-1]}}, beat_sum});
    wire signed [ACC_W-1:0] acc_next = acc + beat_ext;

    // ---------------------------------------------------------------
    // sequential
    // ---------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_cnt       <= {A_CNT_W{1'b0}};
            x_cnt       <= {X_CNT_W{1'b0}};
            acc         <= {ACC_W{1'b0}};
            out_c_r     <= {ACC_W{1'b0}};
            out_valid_r <= 1'b0;
        end else begin
            if (x_acc) begin
                xbank[wsel] <= in_x_flat;
                x_cnt       <= x_cnt + 1'b1;
            end

            if (a_acc) begin
                a_cnt <= a_cnt + 1'b1;
                if (last_beat) begin
                    acc     <= {ACC_W{1'b0}};
                    out_c_r <= acc_next;
                end else begin
                    acc <= acc_next;
                end
            end

            out_valid_r <= last_beat | (out_valid_r & ~out_ready);

            if (txn_end) begin
                a_cnt <= {A_CNT_W{1'b0}};
                x_cnt <= {X_CNT_W{1'b0}};
                acc   <= {ACC_W{1'b0}};
            end
        end
    end

    assign out_valid = out_valid_r;
    assign out_c     = out_c_r;

endmodule
