// 001_dot_product: c = sum_i a[i]*b[i], signed int8 inputs, exact int32 result.
// Two independent valid/ready input streams (LEN elements, LANES per beat),
// one output word per transaction, back-to-back transactions without reset.
module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
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

    localparam BEATS = LEN / LANES;                  // beats per port per transaction
    localparam CNT_W = (BEATS > 1) ? $clog2(BEATS) : 1;

    // One-entry holding registers for beats whose mate has not arrived yet.
    reg [LANES*DATA_W-1:0] a_reg, b_reg;
    reg                    a_stored, b_stored;

    reg signed [ACC_W-1:0] acc;
    reg [CNT_W-1:0]        pairs_done;
    reg signed [ACC_W-1:0] out_reg;
    reg                    out_valid_r;

    wire last_pair   = (pairs_done == BEATS-1);
    // Do not let a new final pair overwrite an output word still awaiting acceptance.
    wire out_blocked = out_valid_r & ~out_ready;
    wire pair_fire   = a_stored & b_stored & ~(last_pair & out_blocked);

    // Pop and push in the same cycle: sustained 1 beat-pair per cycle.
    assign in_a_flat_ready = ~a_stored | pair_fire;
    assign in_b_flat_ready = ~b_stored | pair_fire;

    wire push_a = in_a_flat_valid & in_a_flat_ready;
    wire push_b = in_b_flat_valid & in_b_flat_ready;

    // Balanced 32-lane signed multiply / adder tree over the held beat pair,
    // each level sized exactly: products are 16b, sums grow one bit per level.
    reg signed [15:0] prod [0:31];
    reg signed [16:0] s1   [0:15];
    reg signed [17:0] s2   [0:7];
    reg signed [18:0] s3   [0:3];
    reg signed [19:0] s4   [0:1];
    reg signed [20:0] pair_sum;
    integer i;
    always @* begin
        for (i = 0; i < 32; i = i + 1)
            prod[i] = $signed(a_reg[i*DATA_W +: DATA_W]) * $signed(b_reg[i*DATA_W +: DATA_W]);
        for (i = 0; i < 16; i = i + 1) s1[i] = prod[2*i] + prod[2*i+1];
        for (i = 0; i < 8;  i = i + 1) s2[i] = s1[2*i] + s1[2*i+1];
        for (i = 0; i < 4;  i = i + 1) s3[i] = s2[2*i] + s2[2*i+1];
        for (i = 0; i < 2;  i = i + 1) s4[i] = s3[2*i] + s3[2*i+1];
        pair_sum = s4[0] + s4[1];
    end

    // Sign-extend into accumulator width through a signed net (avoids the
    // unsigned-conditional zero-extension pitfall).
    wire signed [ACC_W-1:0] pair_sum_ext = pair_sum;
    // First pair of a transaction starts a fresh accumulator.
    wire signed [ACC_W-1:0] sum_next =
        (pairs_done == {CNT_W{1'b0}}) ? pair_sum_ext : (acc + pair_sum_ext);

    // Mealy output: the final result is available the same cycle the last
    // beat-pair sits in the holding registers; register it only if not taken.
    wire mealy_valid = a_stored & b_stored & last_pair & ~out_valid_r;
    assign out_valid = out_valid_r | mealy_valid;
    assign out_c     = out_valid_r ? out_reg : sum_next;

    wire out_valid_next = (pair_fire & last_pair & (out_valid_r | ~out_ready))
                        | (out_valid_r & ~out_ready);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_reg       <= {LANES*DATA_W{1'b0}};
            b_reg       <= {LANES*DATA_W{1'b0}};
            a_stored    <= 1'b0;
            b_stored    <= 1'b0;
            acc         <= {ACC_W{1'b0}};
            pairs_done  <= {CNT_W{1'b0}};
            out_reg     <= {ACC_W{1'b0}};
            out_valid_r <= 1'b0;
        end else begin
            if (push_a) begin
                a_reg    <= in_a_flat;
                a_stored <= 1'b1;
            end else if (pair_fire) begin
                a_stored <= 1'b0;
            end
            if (push_b) begin
                b_reg    <= in_b_flat;
                b_stored <= 1'b1;
            end else if (pair_fire) begin
                b_stored <= 1'b0;
            end
            if (pair_fire) begin
                pairs_done <= last_pair ? {CNT_W{1'b0}} : pairs_done + 1'b1;
                acc        <= sum_next;
                if (last_pair) out_reg <= sum_next;
            end
            out_valid_r <= out_valid_next;
        end
    end

endmodule
