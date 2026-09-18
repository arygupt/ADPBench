// Audit fixture: executable constructs appear only inside comments.
// No initial blocks or $display statements are used. There is no #delay
// control, no force/release, no `include, and no reference to tb. or u_dut.
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
    assign in_a_flat_ready = 1'b1;
    assign in_b_flat_ready = 1'b1;
    assign out_valid       = 1'b0;
    assign out_c           = {ACC_W{1'b0}};
endmodule
