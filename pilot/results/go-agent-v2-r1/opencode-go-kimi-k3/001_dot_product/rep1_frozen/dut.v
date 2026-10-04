// 001_dot_product: c = sum_i a[i]*b[i], signed int8 inputs, exact int32 result.
//
// Streams deliver LANES elements per beat, LEN/LANES beats per transaction.
// Beat k of port A pairs with beat k of port B (both in-order), so a beat is
// consumed combinationally directly from the input wires on any cycle where
// both valids are high and the design can accept it. Compliant valid/ready
// masters hold data stable while valid && !ready, so no input buffering is
// required; buffering ahead on one stream could never reduce latency anyway
// because the dot product needs both operands.
//
// Each cycle the 32 lane products of one beat are reduced through a balanced
// adder tree (widths grow 17..21 bits, cheaper than a 32-bit chain) and added
// into the accumulator. On the last beat of a transaction the total is
// presented combinationally (fast path) so it can be accepted on that very
// edge; if not taken it is registered and held until accepted.

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

    localparam BEATS = LEN / LANES;                 // 8 beats per transaction
    localparam CW    = (BEATS <= 2) ? 1 : $clog2(BEATS);

    reg  [CW-1:0]           beat_cnt;               // beats consumed this txn
    reg  signed [ACC_W-1:0] acc;                    // running sum
    reg                     out_v;
    reg  signed [ACC_W-1:0] out_reg;

    wire last_beat = (beat_cnt == BEATS-1);

    // A beat can be consumed unless it is the last of a transaction while the
    // previous output word is still waiting (needs out_ready to free it).
    // When out_v==0 the last beat is always consumable (result is registered
    // if not taken), so `allow` only depends on out_ready when out_v==1.
    wire allow        = !last_beat || !out_v || out_ready;
    wire consume      = in_a_flat_valid && in_b_flat_valid && allow;
    wire last_consume = consume && last_beat;

    assign in_a_flat_ready = in_b_flat_valid && allow;
    assign in_b_flat_ready = in_a_flat_valid && allow;

    // Signed 8x8 -> 16 lane product; return width gives a 16-bit context.
    function automatic signed [15:0] mul8(input signed [7:0] a,
                                          input signed [7:0] b);
        mul8 = a * b;
    endfunction

    // 32 lane products, reduced by a balanced tree of minimally-wide adders.
    wire signed [15:0] p  [0:31];
    wire signed [16:0] s1 [0:15];
    wire signed [17:0] s2 [0:7];
    wire signed [18:0] s3 [0:3];
    wire signed [19:0] s4 [0:1];
    wire signed [20:0] s5;

    genvar gi;
    generate
        for (gi = 0; gi < 32; gi = gi + 1) begin : g_p
            assign p[gi] = mul8(in_a_flat[gi*DATA_W +: DATA_W],
                                in_b_flat[gi*DATA_W +: DATA_W]);
        end
        for (gi = 0; gi < 16; gi = gi + 1) begin : g_s1
            assign s1[gi] = p[2*gi] + p[2*gi+1];
        end
        for (gi = 0; gi < 8; gi = gi + 1) begin : g_s2
            assign s2[gi] = s1[2*gi] + s1[2*gi+1];
        end
        for (gi = 0; gi < 4; gi = gi + 1) begin : g_s3
            assign s3[gi] = s2[2*gi] + s2[2*gi+1];
        end
        for (gi = 0; gi < 2; gi = gi + 1) begin : g_s4
            assign s4[gi] = s3[2*gi] + s3[2*gi+1];
        end
    endgenerate
    assign s5 = s4[0] + s4[1];

    // Sign-extend the 21-bit beat sum to the accumulator width.
    wire signed [ACC_W-1:0] beat_sum = {{(ACC_W-21){s5[20]}}, s5};

    // Total including the current beat: shared by acc update and result.
    wire signed [ACC_W-1:0] total = acc + beat_sum;

    // Fast path: on the final consume the fresh total drives out_c directly.
    // Depends only on registers and input valids (never on out_ready).
    assign out_valid = out_v || (last_consume && !out_v);
    assign out_c     = out_v ? out_reg : total;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            beat_cnt <= 0;
            acc      <= 0;
            out_v    <= 1'b0;
            out_reg  <= 0;
        end else begin
            if (out_v && out_ready && !last_consume)
                out_v <= 1'b0;

            if (last_consume) begin
                acc      <= 0;
                beat_cnt <= 0;
                if (out_v) begin
                    // allow implies out_ready: old word is taken this cycle,
                    // queue the fresh one behind it.
                    out_reg <= total;
                    out_v   <= 1'b1;
                end else if (!out_ready) begin
                    // Fast path not taken: hold the result.
                    out_reg <= total;
                    out_v   <= 1'b1;
                end
                // else fast path accepted: out_v stays 0.
            end else if (consume) begin
                acc      <= total;
                beat_cnt <= beat_cnt + 1'b1;
            end
        end
    end

endmodule
