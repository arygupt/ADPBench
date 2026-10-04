// 001_dot_product
// c = sum_i a[i]*b[i] over signed int8 lanes, exact int32 result.
//
// Throughput-oriented design: all 32 lane products of a beat are formed in
// one cycle by an array of signed 8x8 multipliers and reduced by an exact
// minimal-width adder tree (products 16 bits, beat sum 21 bits), then folded
// into a 32-bit accumulator: one beat retires per accepted input pair, 8
// cycles per transaction. The final beat's result bypasses combinationally to
// the output (same-cycle acceptance when out_ready is high); otherwise it
// enters a two-entry in-order queue so back-to-back transactions never stall
// the inputs.

module dut #(
    parameter LEN    = 256,
    parameter LANES  = 32,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam NP    = LEN / LANES;                        // 8 beats/transaction
    localparam CW    = (NP <= 1) ? 1 : $clog2(NP);
    localparam PW    = 2 * DATA_W;                         // signed product bits
    localparam BW    = PW + $clog2(LANES) + 1;             // beat-sum bits (21)

    // ---------------- handshake --------------------------------------------
    wire accept = in_a_flat_valid & in_b_flat_valid;
    assign in_a_flat_ready = accept;
    assign in_b_flat_ready = accept;

    // ---------------- 32 signed products + exact reduction tree -------------
    wire signed [PW-1:0] prod [0:LANES-1];
    genvar l;
    generate
        for (l = 0; l < LANES; l = l + 1) begin : G_MUL
            assign prod[l] = $signed(in_a_flat[l*DATA_W +: DATA_W])
                           * $signed(in_b_flat[l*DATA_W +: DATA_W]);
        end
    endgenerate

    wire signed [PW  :0] t1 [0:LANES/2 -1];                // pairs: 17 b
    wire signed [PW+1:0] t2 [0:LANES/4 -1];                // quads: 18 b
    wire signed [PW+2:0] t3 [0:LANES/8 -1];                // octs : 19 b
    wire signed [PW+3:0] t4 [0:LANES/16-1];                // 16s  : 20 b
    wire signed [BW-1:0] bsum;                             // beat   : 21 b

    genvar g1, g2, g3, g4, g5;
    generate
        for (g1 = 0; g1 < LANES/2; g1 = g1 + 1) begin : G_T1
            assign t1[g1] = prod[2*g1] + prod[2*g1+1];
        end
        for (g2 = 0; g2 < LANES/4; g2 = g2 + 1) begin : G_T2
            assign t2[g2] = t1[2*g2] + t1[2*g2+1];
        end
        for (g3 = 0; g3 < LANES/8; g3 = g3 + 1) begin : G_T3
            assign t3[g3] = t2[2*g3] + t2[2*g3+1];
        end
        for (g4 = 0; g4 < LANES/16; g4 = g4 + 1) begin : G_T4
            assign t4[g4] = t3[2*g4] + t3[2*g4+1];
        end
        assign bsum = t4[0] + t4[1];                       // exact 21-bit sum
    endgenerate

    // ---------------- per-transaction accumulation --------------------------
    reg  [CW-1:0]           cnt;
    reg  signed [ACC_W-1:0] acc;
    wire                    last   = (cnt == (NP[CW-1:0] - 1'b1));
    wire signed [ACC_W-1:0] result = acc + bsum;
    wire                    push   = accept & last;

    // ---------------- output queue (depth 2) with bypass ---------------------
    reg  signed [ACC_W-1:0] q0, q1;
    reg                     v0, v1;

    wire pop         = v0 & out_ready;
    wire bypass_fire = push & ~v0 & out_ready;

    assign out_valid = v0 | push;
    assign out_c     = v0 ? q0 : result;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt <= {CW{1'b0}};
            acc <= {ACC_W{1'b0}};
            q0  <= {ACC_W{1'b0}};
            q1  <= {ACC_W{1'b0}};
            v0  <= 1'b0;
            v1  <= 1'b0;
        end else begin
            if (accept) begin
                cnt <= last ? {CW{1'b0}} : (cnt + 1'b1);
                acc <= last ? {ACC_W{1'b0}} : result;
            end

            if (push & bypass_fire) begin
                // consumed via bypass this cycle; queue untouched
            end else if (push & pop) begin
                if (v1) begin
                    q0 <= q1;
                    q1 <= result;
                end else begin
                    q0 <= result;
                end
                v0 <= 1'b1;
            end else if (push) begin
                if (!v0) begin
                    q0 <= result;
                    v0 <= 1'b1;
                end else begin
                    q1 <= result;
                    v1 <= 1'b1;
                end
            end else if (pop) begin
                if (v1) begin
                    q0 <= q1;
                    v1 <= 1'b0;
                end else begin
                    v0 <= 1'b0;
                end
            end
        end
    end

endmodule
