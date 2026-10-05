// 1-D valid convolution: y[f][o] = sum_{k=0..K-1} x[o+k] * w[f][k]
// Streams: in_x_flat carries IN_LEN samples (LANES per beat),
//          in_w_flat carries F*K taps row-major (LANES per beat).
// Output: F*OUT_POS int32 words, row-major, one word per handshake.
module dut #(
    parameter IN_LEN = 128,
    parameter F = 4,
    parameter K = 4,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_w_flat,
    input  wire                    in_w_flat_valid,
    output wire                    in_w_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    localparam OUT_POS = IN_LEN - K + 1;             // 125
    localparam X_BEATS = IN_LEN / LANES;             // 8
    localparam W_BEATS = (F*K + LANES - 1) / LANES;  // 1
    localparam NB      = K;                          // banks = window slots = 4
    localparam BD      = IN_LEN / NB;                // 32 entries per bank
    localparam WPB     = LANES / NB;                 // writes per bank per beat = 4

    // ---------------- per-transaction state ----------------
    reg [3:0] x_beats;   // x beats accepted for current transaction (0..X_BEATS)
    reg [1:0] w_beats;   // w beats accepted for current transaction (0..W_BEATS)
    reg [6:0] o_cnt;     // output position within row (0..OUT_POS-1)
    reg [1:0] f_cnt;     // filter row (0..F-1)

    wire x_full = (x_beats == X_BEATS[3:0]);
    wire w_full = (w_beats == W_BEATS[1:0]);

    // Throttle: do not accept the next transaction's beats until the current
    // transaction has fully drained (state cleared on its last output).
    assign in_x_flat_ready = rst_n && !x_full;
    assign in_w_flat_ready = rst_n && !w_full;

    wire x_fire = in_x_flat_valid && in_x_flat_ready;
    wire w_fire = in_w_flat_valid && in_w_flat_ready;

    // ---------------- weight storage (one beat, F*K taps) ----------------
    reg [F*K*DATA_W-1:0] w_all;
    always @(posedge clk) begin
        if (w_fire) w_all <= in_w_flat;
    end

    wire [7:0]          wbase = f_cnt * (K*DATA_W);
    wire [K*DATA_W-1:0] w_cur = w_all[wbase +: K*DATA_W];
    wire [1:0]          rot   = o_cnt[1:0];

    // ---------------- x storage: NB banks, sliding window read ----------------
    // bank b holds x[j] for j % NB == b at index j / NB.
    // window x[o..o+3] hits each bank once; bank b supplies slot (b-rot)&3,
    // read index = (o>>2) + (b < o[1:0]).
    wire [NB*2*DATA_W-1:0] prod_flat;

    genvar b;
    generate
        for (b = 0; b < NB; b = b + 1) begin : xb
            localparam [1:0] B2 = b;
            reg [DATA_W-1:0] mem [0:BD-1];
            integer i;
            always @(posedge clk) begin
                if (x_fire) begin
                    for (i = 0; i < WPB; i = i + 1)
                        mem[x_beats*WPB + i] <= in_x_flat[(b + NB*i)*DATA_W +: DATA_W];
                end
            end

            wire [4:0] ridx = o_cnt[6:2] + ((B2 < o_cnt[1:0]) ? 5'd1 : 5'd0);

            wire signed [DATA_W-1:0]   xval = mem[ridx];
            wire [1:0]                 tap_idx = B2 - rot;
            wire signed [DATA_W-1:0]   wtap = w_cur[tap_idx*DATA_W +: DATA_W];
            wire signed [2*DATA_W-1:0] prod = xval * wtap;

            assign prod_flat[b*2*DATA_W +: 2*DATA_W] = prod;
        end
    endgenerate

    // ---------------- sum of K products, exact int32 result ----------------
    wire signed [2*DATA_W-1:0] p0  = prod_flat[0*2*DATA_W +: 2*DATA_W];
    wire signed [2*DATA_W-1:0] p1  = prod_flat[1*2*DATA_W +: 2*DATA_W];
    wire signed [2*DATA_W-1:0] p2  = prod_flat[2*2*DATA_W +: 2*DATA_W];
    wire signed [2*DATA_W-1:0] p3  = prod_flat[3*2*DATA_W +: 2*DATA_W];
    wire signed [2*DATA_W:0]   s01 = p0 + p1;
    wire signed [2*DATA_W:0]   s23 = p2 + p3;
    wire signed [2*DATA_W+1:0] ssum = s01 + s23;

    assign out_c = ssum;

    // ---------------- output handshake ----------------
    // The window at position o needs x[o+o .. o+K-1]; available once enough
    // x beats have been accepted (handles input backpressure).
    wire [7:0] need = o_cnt + K;
    wire data_ok = ({x_beats, 4'd0} >= need);

    assign out_valid = w_full && data_ok;

    wire last_out = (o_cnt == OUT_POS-1) && (f_cnt == F-1);
    wire out_fire = out_valid && out_ready;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_beats <= 4'd0;
            w_beats <= 2'd0;
            o_cnt   <= 7'd0;
            f_cnt   <= 2'd0;
        end else begin
            if (x_fire) x_beats <= x_beats + 4'd1;
            if (w_fire) w_beats <= w_beats + 2'd1;
            if (out_fire) begin
                if (last_out) begin
                    x_beats <= 4'd0;
                    w_beats <= 2'd0;
                    o_cnt   <= 7'd0;
                    f_cnt   <= 2'd0;
                end else if (o_cnt == OUT_POS-1) begin
                    o_cnt <= 7'd0;
                    f_cnt <= f_cnt + 2'd1;
                end else begin
                    o_cnt <= o_cnt + 7'd1;
                end
            end
        end
    end

endmodule
