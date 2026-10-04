// Dot product:  c = sum_i a[i]*b[i]   (signed int8 x signed int8 -> int32 exact)
//
// Architecture:
//   * Each port delivers LEN/LANES = 8 beats of LANES = 32 int8 elements on
//     its own valid/ready stream, so beats of the two ports may arrive at
//     different times.  One beat register per port holds an early beat.
//   * A beat pair is multiplied-accumulated as soon as both are available:
//     either straight from the arriving inputs (streams aligned) or from the
//     beat registers (streams skewed).  So no buffer-fill latency is added
//     when both ports deliver together.
//   * The 32-lane MAC runs in one cycle: signed products are summed with a
//     balanced adder tree and added to the 32-bit accumulator.
//   * A combine is also allowed in the very cycle a pending output is
//     accepted (the accumulator is then reused for the next transaction).
//
// Handshake: ready depends only on internal registers (plus out_ready),
// never on the other port's valid, so the streams stay deadlock free.

module dut #(
    parameter LEN    = 256,
    parameter LANES  = 32,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                     clk,
    input  wire                     rst_n,
    input  wire [LANES*DATA_W-1:0]  in_a_flat,
    input  wire                     in_a_flat_valid,
    output wire                     in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0]  in_b_flat,
    input  wire                     in_b_flat_valid,
    output wire                     in_b_flat_ready,
    output wire                     out_valid,
    input  wire                     out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam BEATS = LEN / LANES;   // beats per transaction per port
    localparam BW    = 3;             // beat counter width (BEATS = 8)

    // ------------------------------------------------------------------
    // state
    // ------------------------------------------------------------------
    reg [LANES*DATA_W-1:0] a_buf, b_buf;
    reg                    a_v, b_v;
    reg signed [ACC_W-1:0] acc;
    reg [BW-1:0]           cnt;
    reg                    out_v;

    // combining is allowed unless an unaccepted output owns the accumulator
    wire permit = (~out_v) | out_ready;

    // A port's buffer slot is free when empty, or when the other port's beat
    // is registered and will be consumed together with it this cycle.
    wire a_rdy = (~a_v) | (permit & b_v);
    wire b_rdy = (~b_v) | (permit & a_v);

    assign in_a_flat_ready = a_rdy;
    assign in_b_flat_ready = b_rdy;

    wire a_fire = in_a_flat_valid & a_rdy;
    wire b_fire = in_b_flat_valid & b_rdy;

    // a beat of a port is available now if buffered or arriving this cycle
    wire a_avail = a_v | a_fire;
    wire b_avail = b_v | b_fire;

    wire do_comb = a_avail & b_avail & permit;

    // an arriving beat must be parked unless it is consumed by this combine
    wire store_a = a_fire & (a_v | (~do_comb));
    wire store_b = b_fire & (b_v | (~do_comb));

    // MAC operands: registered beat if present, else the beat arriving now
    wire [LANES*DATA_W-1:0] vec_a = a_v ? a_buf : in_a_flat;
    wire [LANES*DATA_W-1:0] vec_b = b_v ? b_buf : in_b_flat;

    // ------------------------------------------------------------------
    // 32-lane signed multiply + balanced adder tree
    // ------------------------------------------------------------------
    wire signed [DATA_W-1:0]     pa [0:LANES-1];
    wire signed [DATA_W-1:0]     pb [0:LANES-1];
    wire signed [2*DATA_W-1:0]   pr [0:LANES-1];

    genvar k;
    generate
        for (k = 0; k < LANES; k = k + 1) begin : LANE
            assign pa[k] = vec_a[k*DATA_W +: DATA_W];
            assign pb[k] = vec_b[k*DATA_W +: DATA_W];
            assign pr[k] = pa[k] * pb[k];
        end
    endgenerate

    // reduction tree (LANES = 32 -> five levels)
    wire signed [2*DATA_W  :0] s1 [0:LANES/2 - 1];
    wire signed [2*DATA_W+1:0] s2 [0:LANES/4 - 1];
    wire signed [2*DATA_W+2:0] s3 [0:LANES/8 - 1];
    wire signed [2*DATA_W+3:0] s4 [0:LANES/16-1];
    wire signed [2*DATA_W+4:0] s5;

    genvar j;
    generate
        for (j = 0; j < LANES/2; j = j + 1) begin : T1
            assign s1[j] = pr[2*j] + pr[2*j+1];
        end
    endgenerate
    generate
        for (j = 0; j < LANES/4; j = j + 1) begin : T2
            assign s2[j] = s1[2*j] + s1[2*j+1];
        end
    endgenerate
    generate
        for (j = 0; j < LANES/8; j = j + 1) begin : T3
            assign s3[j] = s2[2*j] + s2[2*j+1];
        end
    endgenerate
    generate
        for (j = 0; j < LANES/16; j = j + 1) begin : T4
            assign s4[j] = s3[2*j] + s3[2*j+1];
        end
    endgenerate
    assign s5 = s4[0] + s4[1];

    // accumulator input: fresh when the pending output is accepted this cycle
    wire signed [ACC_W-1:0] czero    = {ACC_W{1'b0}};
    wire signed [ACC_W-1:0] acc_base = out_v ? czero : acc;
    wire signed [ACC_W-1:0] acc_next = acc_base + s5;

    // ------------------------------------------------------------------
    // control / accumulate
    // ------------------------------------------------------------------
    assign out_valid = out_v;
    assign out_c     = acc;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_v   <= 1'b0;
            b_v   <= 1'b0;
            acc   <= {ACC_W{1'b0}};
            cnt   <= {BW{1'b0}};
            out_v <= 1'b0;
        end else begin
            // beat slots: park an arriving beat, release a consumed one
            if (store_a) begin
                a_buf <= in_a_flat;
                a_v   <= 1'b1;
            end else if (do_comb) begin
                a_v <= 1'b0;
            end

            if (store_b) begin
                b_buf <= in_b_flat;
                b_v   <= 1'b1;
            end else if (do_comb) begin
                b_v <= 1'b0;
            end

            // accumulate / transaction bookkeeping
            if (do_comb) begin
                acc <= acc_next;
                if (cnt == BEATS-1) begin
                    cnt <= {BW{1'b0}};
                end else begin
                    cnt <= cnt + {{(BW-1){1'b0}}, 1'b1};
                end
            end else if (out_v & out_ready) begin
                acc <= {ACC_W{1'b0}};
            end

            // output flag (set has priority; both cannot be true at once)
            if (out_v & out_ready) begin
                out_v <= 1'b0;
            end
            if (do_comb & (cnt == BEATS-1)) begin
                out_v <= 1'b1;
            end
        end
    end

endmodule
