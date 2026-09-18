module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire                    in_valid,
    output wire                    in_ready,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);
assign in_ready = 1'b1;
assign out_valid = 1'b1;
assign out_c = {ACC_W{1'bx}};
endmodule
