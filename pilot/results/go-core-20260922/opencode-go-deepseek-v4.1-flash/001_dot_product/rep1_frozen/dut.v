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

    localparam BEATS = LEN / LANES;
    localparam CNT_W = $clog2(BEATS + 1);
    localparam PROD_W = 2 * DATA_W;

    // Each product is 16 bits. Sum of LANES=32 products fits in 16+5=21 bits,
    // so a 24-bit tree keeps everything exact.
    localparam SUM_W = 24;

    reg [CNT_W-1:0] beat_a, beat_b;
    reg acc_clear, acc_add;
    reg out_valid_r;
    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] result;

    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire b_fire = in_b_flat_valid && in_b_flat_ready;

    assign in_a_flat_ready = (beat_a < BEATS);
    assign in_b_flat_ready = (beat_b < BEATS);

    // Combinational per-lane products and a balanced add tree.
    wire signed [PROD_W-1:0] prod [0:LANES-1];
    genvar g;
    generate
        for (g = 0; g < LANES; g = g + 1) begin : gen_prod
            wire signed [DATA_W-1:0] av = in_a_flat[g*DATA_W +: DATA_W];
            wire signed [DATA_W-1:0] bv = in_b_flat[g*DATA_W +: DATA_W];
            assign prod[g] = av * bv;
        end
    endgenerate

    // Tree reduction using explicit registers.
    reg signed [SUM_W-1:0] s1 [0:LANES/2-1];
    reg signed [SUM_W-1:0] s2 [0:LANES/4-1];
    reg signed [SUM_W-1:0] s3 [0:LANES/8-1];
    reg signed [SUM_W-1:0] s4 [0:LANES/16-1];
    reg signed [SUM_W-1:0] s5 [0:LANES/32-1];

    integer i, j;
    wire signed [SUM_W-1:0] lane_term;

    always @* begin
        for (i = 0; i < LANES/2; i = i + 1)
            s1[i] = prod[2*i] + prod[2*i+1];
        for (i = 0; i < LANES/4; i = i + 1)
            s2[i] = s1[2*i] + s1[2*i+1];
        for (i = 0; i < LANES/8; i = i + 1)
            s3[i] = s2[2*i] + s2[2*i+1];
        for (i = 0; i < LANES/16; i = i + 1)
            s4[i] = s3[2*i] + s3[2*i+1];
        for (i = 0; i < LANES/32; i = i + 1)
            s5[i] = s4[2*i] + s4[2*i+1];
    end

    assign lane_term = s5[0];

    // Only accumulate when both inputs hand over a beat together.
    wire both_fire = a_fire && b_fire;
    wire last_beat = both_fire && (beat_a == BEATS - 1);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            beat_a <= {CNT_W{1'b0}};
            beat_b <= {CNT_W{1'b0}};
            acc <= {ACC_W{1'b0}};
            result <= {ACC_W{1'b0}};
            out_valid_r <= 1'b0;
        end else begin
            if (a_fire) begin
                if (beat_a == BEATS - 1) beat_a <= {CNT_W{1'b0}};
                else beat_a <= beat_a + 1'b1;
            end
            if (b_fire) begin
                if (beat_b == BEATS - 1) beat_b <= {CNT_W{1'b0}};
                else beat_b <= beat_b + 1'b1;
            end

            if (both_fire) begin
                if (beat_a == BEATS - 1) begin
                    // Final beat: produce result immediately.
                    result <= acc + lane_term;
                    out_valid_r <= 1'b1;
                    acc <= {ACC_W{1'b0}};
                end else begin
                    acc <= acc + lane_term;
                end
            end

            // Output handshake.
            if (out_valid_r && out_ready)
                out_valid_r <= 1'b0;
        end
    end

    assign out_valid = out_valid_r;
    assign out_c = result;

endmodule
