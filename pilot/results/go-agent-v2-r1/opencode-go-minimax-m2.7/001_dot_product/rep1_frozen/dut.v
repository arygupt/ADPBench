// 001_dot_product: c = sum_i a[i] * b[i], signed int8 inputs, exact int32 output
// Two independent valid/ready input streams, 256 elements each, 32 elements per beat
// Back-to-back transactions supported

module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                        clk,
    input  wire                        rst_n,
    input  wire [LANES*DATA_W-1:0]     in_a_flat,
    input  wire                        in_a_flat_valid,
    output wire                        in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0]     in_b_flat,
    input  wire                        in_b_flat_valid,
    output wire                        in_b_flat_ready,
    output wire                        out_valid,
    input  wire                        out_ready,
    output wire signed [ACC_W-1:0]     out_c
);

    localparam NUM_BEATS = LEN / LANES;  // 8 beats

    // State machine
    localparam IDLE = 2'b00;
    localparam RECV = 2'b01;
    localparam DONE = 2'b10;

    reg [1:0] state;
    reg [3:0] beat_cnt;
    reg [ACC_W-1:0] accumulator;
    reg [ACC_W-1:0] out_c_reg;

    // Partial products: one multiplication per lane
    wire [ACC_W-1:0] partial_product [0:LANES-1];

    // Generate multipliers for each lane
    genvar i;
    generate
        for (i = 0; i < LANES; i = i + 1) begin : mul_gen
            wire signed [DATA_W-1:0] a_elem;
            wire signed [DATA_W-1:0] b_elem;
            assign a_elem = in_a_flat[i*DATA_W +: DATA_W];
            assign b_elem = in_b_flat[i*DATA_W +: DATA_W];
            assign partial_product[i] = $signed({{(ACC_W-DATA_W){a_elem[DATA_W-1]}}, a_elem}) *
                                        $signed({{(ACC_W-DATA_W){b_elem[DATA_W-1]}}, b_elem});
        end
    endgenerate

    // Sum of all partial products for current beat
    wire [ACC_W-1:0] sum_partial;
    assign sum_partial = partial_product[0] + partial_product[1] + partial_product[2] + partial_product[3] +
                         partial_product[4] + partial_product[5] + partial_product[6] + partial_product[7] +
                         partial_product[8] + partial_product[9] + partial_product[10] + partial_product[11] +
                         partial_product[12] + partial_product[13] + partial_product[14] + partial_product[15] +
                         partial_product[16] + partial_product[17] + partial_product[18] + partial_product[19] +
                         partial_product[20] + partial_product[21] + partial_product[22] + partial_product[23] +
                         partial_product[24] + partial_product[25] + partial_product[26] + partial_product[27] +
                         partial_product[28] + partial_product[29] + partial_product[30] + partial_product[31];

    // Ready signals - combinational
    always @(*) begin
        case (state)
            IDLE: begin
                in_a_flat_ready = 1'b1;
                in_b_flat_ready = 1'b1;
            end
            RECV: begin
                in_a_flat_ready = in_a_flat_valid;
                in_b_flat_ready = in_b_flat_valid;
            end
            DONE: begin
                in_a_flat_ready = 1'b0;
                in_b_flat_ready = 1'b0;
            end
            default: begin
                in_a_flat_ready = 1'b0;
                in_b_flat_ready = 1'b0;
            end
        endcase
    end

    // Output assignment - hold previous result during RECV
    assign out_c = (state == RECV) ? out_c_reg : accumulator;

    // Sequential logic
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            beat_cnt <= 4'd0;
            accumulator <= {ACC_W{1'b0}};
            out_c_reg <= {ACC_W{1'b0}};
            out_valid <= 1'b0;
        end else begin
            case (state)
                IDLE: begin
                    if (in_a_flat_valid && in_b_flat_valid) begin
                        state <= RECV;
                        beat_cnt <= 4'd1;
                        accumulator <= sum_partial;
                        out_valid <= 1'b0;
                    end else begin
                        state <= IDLE;
                        beat_cnt <= 4'd0;
                        accumulator <= {ACC_W{1'b0}};
                        out_valid <= 1'b0;
                    end
                    out_c_reg <= out_c_reg;
                end

                RECV: begin
                    if (beat_cnt == NUM_BEATS - 1) begin
                        // Last beat - go to DONE with final result
                        accumulator <= accumulator + sum_partial;
                        state <= DONE;
                        beat_cnt <= 4'd0;
                        out_c_reg <= accumulator + sum_partial;  // Latch new result
                        out_valid <= 1'b1;
                    end else begin
                        // Not last beat - continue accumulating
                        if (in_a_flat_valid && in_b_flat_valid) begin
                            accumulator <= accumulator + sum_partial;
                        end
                        beat_cnt <= beat_cnt + 1'b1;
                        state <= RECV;
                        out_valid <= 1'b1;  // Hold previous result valid during computation
                        out_c_reg <= out_c_reg;
                    end
                end

                DONE: begin
                    out_valid <= 1'b1;
                    out_c_reg <= out_c_reg;
                    if (out_ready) begin
                        if (in_a_flat_valid && in_b_flat_valid) begin
                            // Back-to-back: start next transaction
                            state <= RECV;
                            beat_cnt <= 4'd1;
                            accumulator <= sum_partial;
                            out_valid <= 1'b1;  // Keep valid during next computation
                        end else begin
                            // Wait for next transaction
                            state <= IDLE;
                            beat_cnt <= 4'd0;
                            accumulator <= {ACC_W{1'b0}};
                            out_valid <= 1'b0;
                        end
                    end else begin
                        state <= DONE;
                        beat_cnt <= 4'd0;
                        accumulator <= accumulator;
                    end
                end

                default: begin
                    state <= IDLE;
                    beat_cnt <= 4'd0;
                    accumulator <= {ACC_W{1'b0}};
                    out_c_reg <= {ACC_W{1'b0}};
                    out_valid <= 1'b0;
                end
            endcase
        end
    end

endmodule
