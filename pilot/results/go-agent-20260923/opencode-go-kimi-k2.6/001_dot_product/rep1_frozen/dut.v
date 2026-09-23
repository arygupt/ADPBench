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

    localparam BEATS = LEN / LANES;  // 8 beats
    localparam PROD_W = 2 * DATA_W;  // 16 bits for product
    localparam TREE_W = PROD_W + $clog2(LANES);  // 16 + 5 = 21 bits

    reg [LANES*DATA_W-1:0] a_pending;
    reg [LANES*DATA_W-1:0] b_pending;
    reg a_has_pending;
    reg b_has_pending;
    reg [$clog2(BEATS)-1:0] proc_idx;
    reg signed [ACC_W-1:0] accum;
    reg [$clog2(BEATS):0] beats_done;
    reg out_valid_reg;

    wire a_beat_accept = in_a_flat_valid && in_a_flat_ready;
    wire b_beat_accept = in_b_flat_valid && in_b_flat_ready;

    wire can_compute = a_has_pending && b_has_pending;

    assign in_a_flat_ready = !a_has_pending && !out_valid_reg;
    assign in_b_flat_ready = !b_has_pending && !out_valid_reg;

    // Partial product computation - combinational tree
    // int8 * int8 = int16
    wire signed [PROD_W-1:0] prod [0:LANES-1];
    
    genvar g;
    generate
        for (g = 0; g < LANES; g = g + 1) begin : mult_gen
            wire signed [DATA_W-1:0] a_elem;
            wire signed [DATA_W-1:0] b_elem;
            assign a_elem = a_pending[g*DATA_W +: DATA_W];
            assign b_elem = b_pending[g*DATA_W +: DATA_W];
            assign prod[g] = a_elem * b_elem;
        end
    endgenerate

    // Tree reduction with minimal width
    // Level 1: 16-bit -> 17-bit
    wire signed [PROD_W:0] sum_l1 [0:LANES/2-1];
    generate
        for (g = 0; g < LANES/2; g = g + 1) begin : sum1_gen
            assign sum_l1[g] = prod[g*2] + prod[g*2+1];
        end
    endgenerate

    // Level 2: 17-bit -> 18-bit
    wire signed [PROD_W+1:0] sum_l2 [0:LANES/4-1];
    generate
        for (g = 0; g < LANES/4; g = g + 1) begin : sum2_gen
            assign sum_l2[g] = sum_l1[g*2] + sum_l1[g*2+1];
        end
    endgenerate

    // Level 3: 18-bit -> 19-bit
    wire signed [PROD_W+2:0] sum_l3 [0:LANES/8-1];
    generate
        for (g = 0; g < LANES/8; g = g + 1) begin : sum3_gen
            assign sum_l3[g] = sum_l2[g*2] + sum_l2[g*2+1];
        end
    endgenerate

    // Level 4: 19-bit -> 20-bit
    wire signed [PROD_W+3:0] sum_l4 [0:LANES/16-1];
    generate
        for (g = 0; g < LANES/16; g = g + 1) begin : sum4_gen
            assign sum_l4[g] = sum_l3[g*2] + sum_l3[g*2+1];
        end
    endgenerate

    // Level 5: 20-bit -> 21-bit
    wire signed [TREE_W-1:0] partial_sum = sum_l4[0] + sum_l4[1];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_has_pending <= 0;
            b_has_pending <= 0;
            a_pending <= 0;
            b_pending <= 0;
            proc_idx <= 0;
            accum <= 0;
            beats_done <= 0;
            out_valid_reg <= 0;
        end else begin
            if (out_valid_reg && out_ready) begin
                out_valid_reg <= 0;
            end

            if (out_valid_reg && out_ready && beats_done == BEATS) begin
                // Reset for next transaction
                a_has_pending <= 0;
                b_has_pending <= 0;
                proc_idx <= 0;
                accum <= 0;
                beats_done <= 0;
            end else begin
                // Accept A beat
                if (a_beat_accept) begin
                    a_pending <= in_a_flat;
                    a_has_pending <= 1;
                end

                // Accept B beat
                if (b_beat_accept) begin
                    b_pending <= in_b_flat;
                    b_has_pending <= 1;
                end

                // Compute when both pending
                if (can_compute) begin
                    accum <= accum + {{(ACC_W-TREE_W){partial_sum[TREE_W-1]}}, partial_sum};
                    a_has_pending <= 0;
                    b_has_pending <= 0;
                    proc_idx <= proc_idx + 1;
                    beats_done <= beats_done + 1;

                    if (beats_done == BEATS - 1) begin
                        out_valid_reg <= 1;
                    end
                end
            end
        end
    end

    assign out_valid = out_valid_reg;
    assign out_c = accum;

endmodule
