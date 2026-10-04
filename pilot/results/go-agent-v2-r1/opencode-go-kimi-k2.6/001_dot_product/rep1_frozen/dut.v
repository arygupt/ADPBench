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

    reg [LANES*DATA_W-1:0] a_buf;
    reg [LANES*DATA_W-1:0] b_buf;
    reg a_buf_valid;
    reg b_buf_valid;
    
    reg processing;
    reg [4:0] lane_cnt;
    reg [2:0] beat_cnt;
    reg signed [ACC_W-1:0] acc;
    reg out_valid_reg;
    reg signed [ACC_W-1:0] out_c_reg;

    wire a_ready = !a_buf_valid;
    wire b_ready = !b_buf_valid;
    assign in_a_flat_ready = a_ready;
    assign in_b_flat_ready = b_ready;
    assign out_valid = out_valid_reg;
    assign out_c = out_c_reg;

    wire signed [DATA_W-1:0] a_elem = a_buf[lane_cnt * DATA_W +: DATA_W];
    wire signed [DATA_W-1:0] b_elem = b_buf[lane_cnt * DATA_W +: DATA_W];
    wire signed [2*DATA_W-1:0] prod = a_elem * b_elem;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_buf <= {LANES*DATA_W{1'b0}};
            b_buf <= {LANES*DATA_W{1'b0}};
            a_buf_valid <= 1'b0;
            b_buf_valid <= 1'b0;
            processing <= 1'b0;
            lane_cnt <= 5'd0;
            beat_cnt <= 3'd0;
            acc <= {ACC_W{1'b0}};
            out_valid_reg <= 1'b0;
            out_c_reg <= {ACC_W{1'b0}};
        end else begin
            if (in_a_flat_valid && in_a_flat_ready) begin
                a_buf <= in_a_flat;
                a_buf_valid <= 1'b1;
            end
            if (in_b_flat_valid && in_b_flat_ready) begin
                b_buf <= in_b_flat;
                b_buf_valid <= 1'b1;
            end

            if (processing) begin
                acc <= acc + prod;
                
                if (lane_cnt == LANES - 1) begin
                    processing <= 1'b0;
                    a_buf_valid <= 1'b0;
                    b_buf_valid <= 1'b0;
                    
                    if (beat_cnt == BEATS - 1) begin
                        out_c_reg <= acc + prod;
                        out_valid_reg <= 1'b1;
                        beat_cnt <= 3'd0;
                        acc <= {ACC_W{1'b0}};
                    end else begin
                        beat_cnt <= beat_cnt + 1'b1;
                    end
                end else begin
                    lane_cnt <= lane_cnt + 1'b1;
                end
            end else begin
                if (out_valid_reg && out_ready) begin
                    out_valid_reg <= 1'b0;
                end

                if (a_buf_valid && b_buf_valid) begin
                    processing <= 1'b1;
                    lane_cnt <= 5'd0;
                end
            end
        end
    end

endmodule
