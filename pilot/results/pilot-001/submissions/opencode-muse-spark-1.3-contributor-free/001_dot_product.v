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

    reg signed [ACC_W-1:0] acc;
    reg [8:0] cnt;
    reg out_valid_r;
    reg signed [ACC_W-1:0] out_c_r;

    wire need;
    assign need = (cnt < BEATS);

    assign in_a_flat_ready = need && !out_valid_r && in_b_flat_valid;
    assign in_b_flat_ready = need && !out_valid_r && in_a_flat_valid;

    assign out_valid = out_valid_r;
    assign out_c = out_c_r;

    genvar gi;
    wire signed [2*DATA_W-1:0] prod_arr [0:LANES-1];
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : gen_prod
            wire signed [DATA_W-1:0] a_elem = in_a_flat[gi*DATA_W +: DATA_W];
            wire signed [DATA_W-1:0] b_elem = in_b_flat[gi*DATA_W +: DATA_W];
            assign prod_arr[gi] = a_elem * b_elem;
        end
    endgenerate

    reg signed [ACC_W-1:0] psum;
    integer k;
    always @* begin
        psum = 0;
        for (k = 0; k < LANES; k = k + 1) begin
            psum = psum + prod_arr[k];
        end
    end

    wire last_beat;
    assign last_beat = (cnt == (BEATS - 1));

    wire accept;
    assign accept = need && !out_valid_r && in_a_flat_valid && in_b_flat_valid;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc <= 0;
            cnt <= 0;
            out_valid_r <= 0;
            out_c_r <= 0;
        end else begin
            if (out_valid_r) begin
                if (out_ready) begin
                    out_valid_r <= 0;
                    out_c_r <= 0;
                    acc <= 0;
                    cnt <= 0;
                end
            end else if (accept) begin
                if (last_beat) begin
                    out_c_r <= acc + psum;
                    acc <= acc + psum;
                    cnt <= cnt + 1;
                    out_valid_r <= 1;
                end else begin
                    acc <= acc + psum;
                    cnt <= cnt + 1;
                end
            end
        end
    end

endmodule
