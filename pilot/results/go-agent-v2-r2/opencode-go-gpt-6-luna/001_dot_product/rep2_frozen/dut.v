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
    output reg                     out_valid,
    input  wire                    out_ready,
    output reg signed [ACC_W-1:0]  out_c
);

    localparam GROUP = 16;
    localparam GROUP_SUM_W = 2*DATA_W + $clog2(GROUP);

    reg [LANES*DATA_W-1:0] a_buf;
    reg [LANES*DATA_W-1:0] b_buf;
    reg a_full;
    reg b_full;
    reg processing;
    reg [31:0] lane_idx;
    reg [31:0] pair_count;
    reg signed [ACC_W-1:0] accum;

    function automatic signed [GROUP_SUM_W-1:0] group_dot;
        input [LANES*DATA_W-1:0] a;
        input [LANES*DATA_W-1:0] b;
        integer i;
        reg signed [DATA_W-1:0] av;
        reg signed [DATA_W-1:0] bv;
        reg signed [2*DATA_W-1:0] product;
        reg signed [GROUP_SUM_W-1:0] total;
        begin
            total = {GROUP_SUM_W{1'b0}};
            for (i = 0; i < GROUP; i = i + 1) begin
                av = a[i*DATA_W +: DATA_W];
                bv = b[i*DATA_W +: DATA_W];
                product = av * bv;
                total = total + product;
            end
            group_dot = total;
        end
    endfunction

    wire signed [ACC_W-1:0] group_sum = group_dot(a_buf, b_buf);

    assign in_a_flat_ready = !a_full && !out_valid;
    assign in_b_flat_ready = !b_full && !out_valid;

    always @(posedge clk) begin
        if (!rst_n) begin
            a_buf <= {LANES*DATA_W{1'b0}};
            b_buf <= {LANES*DATA_W{1'b0}};
            a_full <= 1'b0;
            b_full <= 1'b0;
            processing <= 1'b0;
            lane_idx <= 32'd0;
            pair_count <= 32'd0;
            accum <= {ACC_W{1'b0}};
            out_valid <= 1'b0;
            out_c <= {ACC_W{1'b0}};
        end else begin
            if (out_valid && out_ready)
                out_valid <= 1'b0;

            if (in_a_flat_valid && in_a_flat_ready) begin
                a_buf <= in_a_flat;
                a_full <= 1'b1;
            end
            if (in_b_flat_valid && in_b_flat_ready) begin
                b_buf <= in_b_flat;
                b_full <= 1'b1;
            end

            if (!processing && a_full && b_full && !out_valid) begin
                processing <= 1'b1;
                lane_idx <= 32'd0;
            end

            if (processing) begin
                a_buf <= a_buf >> (GROUP*DATA_W);
                b_buf <= b_buf >> (GROUP*DATA_W);
                accum <= accum + group_sum;
                if (lane_idx == (LANES - GROUP)) begin
                    processing <= 1'b0;
                    a_full <= 1'b0;
                    b_full <= 1'b0;
                    if (pair_count == (LEN/LANES - 1)) begin
                        out_c <= accum + group_sum;
                        out_valid <= 1'b1;
                        accum <= {ACC_W{1'b0}};
                        pair_count <= 32'd0;
                    end else begin
                        pair_count <= pair_count + 32'd1;
                    end
                end else begin
                    lane_idx <= lane_idx + GROUP;
                end
            end
        end
    end

endmodule
