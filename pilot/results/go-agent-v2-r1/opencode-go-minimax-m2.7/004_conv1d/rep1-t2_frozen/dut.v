// ADPBench submission. 1-D convolution with LANES=16 parallelism.
// y[f][o] = sum_k x[o+k] * w[f][k] for each filter f and output position o.
// Pipelined computation and output for better throughput.

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

    // Constants
    localparam OUT_POS = IN_LEN - K + 1;  // 125
    localparam X_BEATS = (IN_LEN + LANES - 1) / LANES;  // 8 beats for 128 elements
    localparam TOTAL_OUTPUTS = F * OUT_POS;  // 500 outputs

    // State machine
    localparam IDLE = 2'b00;
    localparam ACCEPT = 2'b01;
    localparam COMPUTE = 2'b10;
    localparam OUTPUT = 2'b11;

    reg [1:0] state;

    // Input buffers
    reg signed [7:0] x_buf [0:IN_LEN-1];  // 128 int8 samples
    reg signed [7:0] w_buf [0:F*K-1];    // 16 int8 weights (row-major filters)

    // Input counters
    reg [3:0] x_beat_cnt;    // Counts 0-7 for 8 beats of x input
    reg x_done, w_done;      // Flags for input completion

    // Computation state
    reg signed [ACC_W-1:0] acc;  // Accumulator for current f,o
    reg [3:0] k_cnt;             // Inner loop: 0-3 for K=4 multiplications
    reg [6:0] pos_cnt;           // Output position: 0-124
    reg [1:0] f_cnt;             // Filter index: 0-3
    reg comp_done;               // Computation complete flag

    // Output state
    reg [8:0] out_cnt;           // Total outputs sent (0-499)
    reg [1:0] out_f;             // Current filter to output
    reg [6:0] out_pos;           // Current position to output
    reg [3:0] out_k;             // Current k within recomputation
    reg out_valid_reg;
    reg [ACC_W-1:0] out_c_reg;
    reg out_result_valid;        // Result ready to be output
    reg [ACC_W-1:0] out_result;   // Cached result

    // Ready signals
    reg in_x_flat_ready_int;
    reg in_w_flat_ready_int;

    // Output assignments
    assign in_x_flat_ready = in_x_flat_ready_int;
    assign in_w_flat_ready = in_w_flat_ready_int;
    assign out_valid = out_valid_reg;
    assign out_c = out_c_reg;

    // State transitions
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
        end else begin
            case (state)
                IDLE: begin
                    if (in_x_flat_valid && in_w_flat_valid)
                        state <= ACCEPT;
                end
                ACCEPT: begin
                    if (x_done && w_done)
                        state <= COMPUTE;
                end
                COMPUTE: begin
                    if (comp_done)
                        state <= OUTPUT;
                end
                OUTPUT: begin
                    if (out_cnt == TOTAL_OUTPUTS && out_ready)
                        state <= IDLE;
                end
                default: state <= IDLE;
            endcase
        end
    end

    // Input acceptance logic
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_beat_cnt <= 4'd0;
            x_done <= 1'b0;
            w_done <= 1'b0;
            in_x_flat_ready_int <= 1'b1;
            in_w_flat_ready_int <= 1'b1;
        end else begin
            case (state)
                IDLE: begin
                    x_beat_cnt <= 4'd0;
                    x_done <= 1'b0;
                    w_done <= 1'b0;
                    in_x_flat_ready_int <= 1'b1;
                    in_w_flat_ready_int <= 1'b1;
                end

                ACCEPT: begin
                    // Accept x_flat: 8 beats of 16 elements each
                    if (in_x_flat_valid && in_x_flat_ready_int && !x_done) begin
                        integer i;
                        for (i = 0; i < LANES; i = i + 1) begin
                            if (x_beat_cnt * LANES + i < IN_LEN)
                                x_buf[x_beat_cnt * LANES + i] <= in_x_flat[i*DATA_W +: DATA_W];
                        end
                        if (x_beat_cnt == X_BEATS - 1) begin
                            x_done <= 1'b1;
                            in_x_flat_ready_int <= 1'b0;
                        end else begin
                            x_beat_cnt <= x_beat_cnt + 1'b1;
                        end
                    end

                    // Accept w_flat: 1 beat of 16 elements
                    if (in_w_flat_valid && in_w_flat_ready_int && !w_done) begin
                        integer i;
                        for (i = 0; i < F*K; i = i + 1) begin
                            w_buf[i] <= in_w_flat[i*DATA_W +: DATA_W];
                        end
                        w_done <= 1'b1;
                        in_w_flat_ready_int <= 1'b0;
                    end
                end

                default: begin
                    in_x_flat_ready_int <= 1'b0;
                    in_w_flat_ready_int <= 1'b0;
                end
            endcase
        end
    end

    // Computation logic - time-multiplexed accumulator
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            k_cnt <= 4'd0;
            pos_cnt <= 7'd0;
            f_cnt <= 2'd0;
            acc <= 32'd0;
            comp_done <= 1'b0;
        end else begin
            case (state)
                IDLE: begin
                    k_cnt <= 4'd0;
                    pos_cnt <= 7'd0;
                    f_cnt <= 2'd0;
                    acc <= 32'd0;
                    comp_done <= 1'b0;
                end

                ACCEPT: begin
                    comp_done <= 1'b0;
                end

                COMPUTE: begin
                    if (!comp_done) begin
                        // MAC: acc += x[pos+k] * w[f][k]
                        acc <= acc + $signed(x_buf[pos_cnt + k_cnt]) * $signed(w_buf[f_cnt * K + k_cnt]);
                        k_cnt <= k_cnt + 1'b1;

                        // K iterations done
                        if (k_cnt == K - 1) begin
                            k_cnt <= 4'd0;

                            // Advance position/filter
                            if (pos_cnt < OUT_POS - 1) begin
                                pos_cnt <= pos_cnt + 1'b1;
                                acc <= 32'd0;
                            end else if (f_cnt < F - 1) begin
                                f_cnt <= f_cnt + 1'b1;
                                pos_cnt <= 7'd0;
                                acc <= 32'd0;
                            end else begin
                                comp_done <= 1'b1;
                            end
                        end
                    end
                end

                default: begin
                    // Keep state
                end
            endcase
        end
    end

    // Output logic - pipelined recomputation
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out_cnt <= 9'd0;
            out_f <= 2'd0;
            out_pos <= 7'd0;
            out_k <= 4'd0;
            out_valid_reg <= 1'b0;
            out_c_reg <= 32'd0;
            out_result_valid <= 1'b0;
            out_result <= 32'd0;
        end else begin
            case (state)
                IDLE: begin
                    out_cnt <= 9'd0;
                    out_f <= 2'd0;
                    out_pos <= 7'd0;
                    out_k <= 4'd0;
                    out_valid_reg <= 1'b0;
                    out_c_reg <= 32'd0;
                    out_result_valid <= 1'b0;
                    out_result <= 32'd0;
                end

                COMPUTE: begin
                    out_valid_reg <= 1'b0;
                    out_result_valid <= 1'b0;
                end

                OUTPUT: begin
                    // Pipeline: compute next while outputting current
                    if (out_result_valid && out_ready) begin
                        // Output consumed, advance
                        out_cnt <= out_cnt + 1'b1;
                        out_result_valid <= 1'b0;

                        // Advance to next (f, pos)
                        if (out_pos < OUT_POS - 1) begin
                            out_pos <= out_pos + 1'b1;
                        end else if (out_f < F - 1) begin
                            out_f <= out_f + 1'b1;
                            out_pos <= 7'd0;
                        end
                    end

                    // Compute phase: accumulate K terms
                    if (out_k < K) begin
                        out_c_reg <= out_c_reg + $signed(x_buf[out_pos + out_k]) * $signed(w_buf[out_f * K + out_k]);
                        out_k <= out_k + 1'b1;
                    end else if (!out_result_valid) begin
                        // K terms done, result ready
                        out_result <= out_c_reg;
                        out_result_valid <= 1'b1;
                        out_k <= 4'd0;
                        out_c_reg <= 32'd0;
                    end

                    // Output the cached result
                    out_valid_reg <= out_result_valid;
                    if (out_result_valid)
                        out_c_reg <= out_result;
                end

                default: begin
                    out_valid_reg <= 1'b0;
                end
            endcase
        end
    end

endmodule
