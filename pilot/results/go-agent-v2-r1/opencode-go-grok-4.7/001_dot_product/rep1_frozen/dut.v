module dut #(
    parameter LEN    = 256,
    parameter LANES  = 32,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
) (
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

    localparam integer BEATS  = LEN / LANES;
    localparam integer CNT_W  = (BEATS <= 1) ? 1 : $clog2(BEATS);
    localparam integer PROD_W = DATA_W * 2;

    reg signed [ACC_W-1:0] acc;
    reg [CNT_W-1:0]        beat_cnt;
    reg                    out_pending;
    reg signed [ACC_W-1:0] out_hold;

    wire is_last   = (beat_cnt == BEATS - 1);
    // Accept a non-final beat even while a result is waiting. The final beat
    // needs the output slot, which frees on the cycle out_ready takes it.
    wire mac_ready = ~is_last | ~out_pending | out_ready;

    assign in_a_flat_ready = in_b_flat_valid & mac_ready;
    assign in_b_flat_ready = in_a_flat_valid & mac_ready;

    wire in_fire   = in_a_flat_valid & in_b_flat_valid & mac_ready;
    wire last_fire = in_fire & is_last;

    // 8x8 signed products, explicitly 16 bits so synthesis keeps a narrow mul.
    wire signed [PROD_W-1:0] prod [0:LANES-1];

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_mul
            wire signed [DATA_W-1:0] a_s;
            wire signed [DATA_W-1:0] b_s;
            assign a_s = $signed(in_a_flat[gi*DATA_W +: DATA_W]);
            assign b_s = $signed(in_b_flat[gi*DATA_W +: DATA_W]);
            assign prod[gi] = a_s * b_s;
        end
    endgenerate

    // Balanced-enough reduction. Width grows only as far as the beat sum needs.
    // Max |beat sum| = LANES * 128 * 128, which fits in PROD_W+$clog2(LANES)+1.
    localparam integer SUM_W = PROD_W + $clog2(LANES) + 1;

    reg signed [SUM_W-1:0] beat_sum;
    integer i;
    always @(*) begin
        beat_sum = {SUM_W{1'b0}};
        for (i = 0; i < LANES; i = i + 1)
            beat_sum = beat_sum + prod[i];
    end

    wire signed [ACC_W-1:0] acc_base = (beat_cnt == {CNT_W{1'b0}}) ? {ACC_W{1'b0}} : acc;
    wire signed [ACC_W-1:0] sum_now  = acc_base + beat_sum;

    assign out_valid = out_pending | last_fire;
    assign out_c     = out_pending ? out_hold : (last_fire ? sum_now : {ACC_W{1'b0}});

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc         <= {ACC_W{1'b0}};
            beat_cnt    <= {CNT_W{1'b0}};
            out_pending <= 1'b0;
            out_hold    <= {ACC_W{1'b0}};
        end else if (last_fire) begin
            beat_cnt <= {CNT_W{1'b0}};
            acc      <= {ACC_W{1'b0}};
            // Old result (if any) is the word transferred this cycle. Park the
            // new sum unless it was itself consumed combinationally.
            if (out_pending || !out_ready) begin
                out_hold    <= sum_now;
                out_pending <= 1'b1;
            end else begin
                out_pending <= 1'b0;
            end
        end else begin
            if (in_fire) begin
                beat_cnt <= beat_cnt + 1'b1;
                acc      <= sum_now;
            end
            if (out_pending && out_ready)
                out_pending <= 1'b0;
        end
    end

endmodule
