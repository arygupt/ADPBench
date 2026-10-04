// 004_conv1d: 1-D valid convolution, exact int32.
// y[f][o] = sum_k x[o+k] * w[f][k],  x: IN_LEN int8, w: F x K int8,
// y: F x OUT_POS int32, row-major output stream.
//
// Architecture: x is held in a 128 x 8 rotating register.  During the
// compute phase the register rotates one byte per cycle, so at compute
// cycle T position i holds x[(i+T) mod 128].  The 4-tap FIR is evaluated
// from four fixed positions of the rotating register: at cycle T the
// window {IN_LEN-3, IN_LEN-2, IN_LEN-1, 0} holds x[T-3], x[T-2], x[T-1],
// x[T], giving y[T-3].  The sum is registered, so the word presented
// during cycle T is y[T-4].  One output word is produced per cycle.

module dut #(
    parameter IN_LEN  = 128,
    parameter F       = 4,
    parameter K       = 4,
    parameter LANES   = 16,
    parameter DATA_W  = 8,
    parameter ACC_W   = 32
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

    localparam integer X_BEATS = IN_LEN / LANES;                 // 8
    localparam integer W_BEATS = (F * K) / LANES;                // 1
    localparam integer OUT_POS = IN_LEN - K + 1;                 // 125
    localparam integer T_LAST  = (F - 1) * IN_LEN + K + OUT_POS - 1; // 512
    localparam integer LOG_IN  = 7;                              // log2(IN_LEN)

    // ---------------- state ----------------
    reg        state;        // 0 = RX, 1 = COMPUTE
    reg [9:0]  T;            // compute cycle counter
    reg signed [ACC_W-1:0] acc;
    reg [3:0]  x_beat;       // x beats accepted (0..X_BEATS)
    reg        w_beat;       // w beats accepted (0..W_BEATS)
    reg [DATA_W-1:0] xreg [0:IN_LEN-1];
    reg [DATA_W-1:0] wreg [0:F*K-1];

    // ---------------- input handshakes ----------------
    wire x_accept = (state == 1'b0) && in_x_flat_valid && in_x_flat_ready;
    wire w_accept = (state == 1'b0) && in_w_flat_valid && in_w_flat_ready;

    assign in_x_flat_ready = (state == 1'b0) && (x_beat < X_BEATS);
    assign in_w_flat_ready = (state == 1'b0) && (w_beat < W_BEATS);

    wire x_done = (x_beat == X_BEATS) || (x_accept && x_beat == X_BEATS - 1);
    wire w_done = (w_beat == W_BEATS) || (w_accept && w_beat == W_BEATS - 1);

    // ---------------- output ----------------
    wire [6:0] u = T[6:0];                 // T mod IN_LEN
    assign out_valid = (state == 1'b1) &&
                       ((u >= K) || (u == 0 && T >= IN_LEN));
    assign out_c = acc;

    wire stall   = (state == 1'b1) && out_valid && !out_ready;
    wire comp_en = (state == 1'b1) && !stall && (T < T_LAST);

    // ---------------- weights (row-major F x K) ----------------
    wire [1:0] fidx = (T >= K - 1) ? ((T - (K - 1)) >> LOG_IN) : 2'd0;
    wire signed [DATA_W-1:0] w0 = wreg[{fidx, 2'b00}];
    wire signed [DATA_W-1:0] w1 = wreg[{fidx, 2'b01}];
    wire signed [DATA_W-1:0] w2 = wreg[{fidx, 2'b10}];
    wire signed [DATA_W-1:0] w3 = wreg[{fidx, 2'b11}];

    // ---------------- x window from the rotating register ----------------
    wire signed [DATA_W-1:0] x125 = xreg[IN_LEN - 3];
    wire signed [DATA_W-1:0] x126 = xreg[IN_LEN - 2];
    wire signed [DATA_W-1:0] x127 = xreg[IN_LEN - 1];
    wire signed [DATA_W-1:0] x000 = xreg[0];

    // 16-bit signed products, then explicit sign extension to ACC_W
    wire signed [2*DATA_W-1:0] m0 = x125 * w0;
    wire signed [2*DATA_W-1:0] m1 = x126 * w1;
    wire signed [2*DATA_W-1:0] m2 = x127 * w2;
    wire signed [2*DATA_W-1:0] m3 = x000 * w3;

    wire signed [ACC_W-1:0] p0 = m0;
    wire signed [ACC_W-1:0] p1 = m1;
    wire signed [ACC_W-1:0] p2 = m2;
    wire signed [ACC_W-1:0] p3 = m3;

    wire signed [ACC_W-1:0] fir = p0 + p1 + p2 + p3;

    // ---------------- sequential logic ----------------
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state  <= 1'b0;
            T      <= 10'd0;
            acc    <= 0;
            x_beat <= 4'd0;
            w_beat <= 1'b0;
            for (i = 0; i < IN_LEN; i = i + 1) xreg[i] <= {DATA_W{1'b0}};
            for (i = 0; i < F * K;  i = i + 1) wreg[i] <= {DATA_W{1'b0}};
        end else begin
            // state and counters
            if (state == 1'b0) begin
                T <= 10'd0;
                if (x_done && w_done) state <= 1'b1;
                if (x_accept) x_beat <= x_beat + 4'd1;
                if (w_accept) w_beat <= w_beat + 1'b1;
            end else begin
                if (T == T_LAST) begin
                    if (out_valid && out_ready) begin
                        state <= 1'b0;
                        T     <= 10'd0;
                        acc   <= 0;
                    end
                end else if (!stall) begin
                    T <= T + 10'd1;
                end
                x_beat <= 4'd0;
                w_beat <= 1'b0;
            end

            if (comp_en) acc <= fir;

            // x rotating register: rotate during compute, load beats during RX
            for (i = 0; i < IN_LEN; i = i + 1) begin
                if (comp_en) begin
                    xreg[i] <= (i == IN_LEN - 1) ? xreg[0] : xreg[i + 1];
                end else if (x_accept && (i / LANES == x_beat)) begin
                    xreg[i] <= in_x_flat[i * DATA_W +: DATA_W];
                end
            end

            // weight registers
            if (w_accept) begin
                for (i = 0; i < F * K; i = i + 1)
                    wreg[i] <= in_w_flat[i * DATA_W +: DATA_W];
            end
        end
    end

endmodule
