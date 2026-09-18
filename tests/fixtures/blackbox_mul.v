// Cheat fixture: a blackbox `$mul` survives the pinned flow, simulates via
// simlib, and would count as one cell. Synthesis must reject it.
(* blackbox *)
module \$mul #(
    parameter A_WIDTH = 8,
    parameter B_WIDTH = 8,
    parameter Y_WIDTH = 16
)(
    input  wire [A_WIDTH-1:0] A,
    input  wire [B_WIDTH-1:0] B,
    output wire [Y_WIDTH-1:0] Y
);
endmodule

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
    wire [15:0] prod;
    \$mul #(.A_WIDTH(8), .B_WIDTH(8), .Y_WIDTH(16)) m0 (
        .A(in_a_flat[7:0]), .B(in_b_flat[7:0]), .Y(prod)
    );
    assign in_a_flat_ready = 1'b1;
    assign in_b_flat_ready = 1'b1;
    assign out_valid       = 1'b0;
    assign out_c           = prod;
endmodule
