// Cheat fixture: the directed `zeros` case expects zero, and this design
// presents zero forever without ever accepting an input beat. It must be
// rejected because the transaction's inputs were never consumed.
module dut #(
    parameter LEN    = 256,
    parameter LANES  = 32,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
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
    assign in_a_flat_ready = 1'b0;
    assign in_b_flat_ready = 1'b0;
    assign out_valid       = 1'b1;
    assign out_c           = {ACC_W{1'b0}};
endmodule
