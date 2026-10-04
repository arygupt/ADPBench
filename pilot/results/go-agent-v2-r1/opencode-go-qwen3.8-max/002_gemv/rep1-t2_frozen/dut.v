`default_nettype none
// GEMV: y = A @ x, A is ROWS x COLS row-major int8, x is COLS int8, y int32 exact.
// Streaming engine: consumes A one beat (LANES elements) per cycle, LANES signed
// multipliers + adder tree, one running accumulator; row results go through a
// 2-slot output buffer so input readies never depend on out_ready.
module dut #(
    parameter ROWS   = 16,
    parameter COLS   = 64,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                     clk,
    input  wire                     rst_n,
    input  wire [LANES*DATA_W-1:0]  in_a_flat,
    input  wire                     in_a_flat_valid,
    output wire                     in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0]  in_x_flat,
    input  wire                     in_x_flat_valid,
    output wire                     in_x_flat_ready,
    output wire                     out_valid,
    input  wire                     out_ready,
    output wire signed [ACC_W-1:0]  out_c
);
    // ---- geometry -------------------------------------------------------
    localparam integer QB     = COLS / LANES;        // A beats per row (also x beats total)
    localparam integer ABEATS = ROWS * QB;           // total A beats per transaction
    localparam integer XBEATS = COLS / LANES;        // total x beats per transaction
    localparam integer AW     = $clog2(ABEATS + 1);
    localparam integer XW     = $clog2(XBEATS + 1);
    localparam integer OW     = $clog2(ROWS + 1);
    localparam integer QW     = $clog2(QB);          // bits for beat-in-row index
    // Exact accumulator width: |sum| <= COLS * 2^(2*DATA_W-2) = 2^20 -> 22 bits signed
    localparam integer ACW    = 2*DATA_W + $clog2(COLS);

    // ---- state -----------------------------------------------------------
    reg  [AW-1:0]             a_cnt;    // A beats consumed (0..ABEATS)
    reg  [XW-1:0]             x_cnt;    // x beats consumed (0..XBEATS)
    reg  [OW-1:0]             o_cnt;    // row results produced (0..ROWS)
    reg  signed [ACW-1:0]     acc;      // running row accumulator
    reg  [ACC_W-1:0]          out_reg;  // output head
    reg  [ACC_W-1:0]          skid_reg; // output skid slot
    reg  [1:0]                oslots;   // number of occupied output slots (0..2)
    reg  [LANES*DATA_W-1:0]   xbuf [0:QB-1];  // stored x vector, QB chunks

    wire [QW-1:0] qidx     = a_cnt[QW-1:0];          // which x chunk this A beat needs
    wire          last_col = (qidx == QB-1);         // final beat of a row

    // x chunk q is available once x_cnt > q
    wire x_avail = (x_cnt > {{(XW-QW){1'b0}}, qidx});

    // ready signals depend only on internal state (never on out_ready / valids)
    assign in_a_flat_ready = rst_n && (a_cnt != ABEATS[AW-1:0]) && x_avail &&
                             (!last_col || (oslots != 2'd2));
    assign in_x_flat_ready = rst_n && (x_cnt != XBEATS[XW-1:0]);

    wire a_hs     = in_a_flat_valid && in_a_flat_ready;
    wire x_hs     = in_x_flat_valid && in_x_flat_ready;
    wire row_done = a_hs && last_col;
    wire out_head_valid = (oslots != 2'd0);
    wire o_hs     = out_head_valid && out_ready;
    wire txn_end  = o_hs && (o_cnt == ROWS[OW-1:0]) && (oslots == 2'd1);

    // ---- combinational datapath -------------------------------------------
    // select the x chunk needed by the current A beat
    reg [LANES*DATA_W-1:0] xs;
    integer m;
    always @* begin
        xs = xbuf[0];
        for (m = 0; m < QB; m = m + 1)
            if (qidx == m[QW-1:0])
                xs = xbuf[m];
    end

    // signed dot product of one beat: LANES int8 x int8 products
    reg signed [ACW-1:0] beat_sum;
    integer k;
    always @* begin
        beat_sum = {ACW{1'b0}};
        for (k = 0; k < LANES; k = k + 1)
            beat_sum = beat_sum +
                       $signed(in_a_flat[k*DATA_W +: DATA_W]) *
                       $signed(xs[k*DATA_W +: DATA_W]);
    end

    wire signed [ACW-1:0] acc_next   = (qidx == 0) ? beat_sum : (acc + beat_sum);
    wire [ACC_W-1:0]      row_result = {{(ACC_W-ACW){acc_next[ACW-1]}}, acc_next};

    // ---- sequential update -------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_cnt    <= {AW{1'b0}};
            x_cnt    <= {XW{1'b0}};
            o_cnt    <= {OW{1'b0}};
            acc      <= {ACW{1'b0}};
            out_reg  <= {ACC_W{1'b0}};
            skid_reg <= {ACC_W{1'b0}};
            oslots   <= 2'd0;
        end else if (txn_end) begin
            // transaction complete: clear per-transaction state
            a_cnt    <= {AW{1'b0}};
            x_cnt    <= {XW{1'b0}};
            o_cnt    <= {OW{1'b0}};
            acc      <= {ACW{1'b0}};
            out_reg  <= {ACC_W{1'b0}};
            skid_reg <= {ACC_W{1'b0}};
            oslots   <= 2'd0;
        end else begin
            if (x_hs) begin
                xbuf[x_cnt[QW-1:0]] <= in_x_flat;
                x_cnt <= x_cnt + 1'b1;
            end
            if (a_hs) begin
                a_cnt <= a_cnt + 1'b1;
                acc   <= acc_next;
            end
            // output slot management
            case ({row_done, o_hs})
                2'b10: begin
                    if (oslots == 2'd0) out_reg <= row_result;
                    else                skid_reg <= row_result;
                    oslots <= oslots + 2'd1;
                    o_cnt  <= o_cnt + 1'b1;
                end
                2'b01: begin
                    if (oslots == 2'd2) out_reg <= skid_reg;
                    oslots <= oslots - 2'd1;
                end
                2'b11: begin
                    if (oslots == 2'd1) out_reg <= row_result;
                    else begin
                        out_reg  <= skid_reg;
                        skid_reg <= row_result;
                    end
                    // oslots unchanged
                    o_cnt <= o_cnt + 1'b1;
                end
                default: ;
            endcase
        end
    end

    assign out_valid = out_head_valid;
    assign out_c     = out_reg;
endmodule
`default_nettype wire
