`default_nettype none
// 1-D valid convolution: y[f][o] = sum_k x[o+k] * w[f][k]
//
// Architecture:
//  - x is loaded into a 128-byte circular shift register (sr). The head of
//    sr always presents the window x[o..o+3]; rotating is pure wiring.
//  - w is loaded into a 16-byte register file (wr), selected per filter row.
//  - The dot product is computed with a bit-plane (transposed) decomposition:
//      acc = sum_j 2^j * S_j   (j=0..6)  -  2^7 * S_7
//      S_j = sum_k x_k[j] ? c_k : 0
//    which needs only AND-mask rows and narrow adders (no $mul cells).
//  - One output word per cycle; after each row of 125 outputs the window is
//    rotated 3 extra cycles (125+3 = 128 = full circle) to restore x[0].
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
    localparam OUT_POS = IN_LEN - K + 1;          // 125
    localparam [3:0] XBEATS = IN_LEN / LANES;     // 8

    localparam [1:0] S_LOAD = 2'd0,
                     S_RUN  = 2'd1,
                     S_REW  = 2'd2;

    reg [1:0] state;
    reg [3:0] xbeat;
    reg       wgot;
    reg [7:0] ocnt;
    reg [1:0] fcnt;
    reg [1:0] rcnt;

    reg [7:0] sr [0:IN_LEN-1];   // raw x bytes, rotating window
    reg [7:0] wr [0:F*K-1];      // raw w bytes

    wire xhs = in_x_flat_valid && in_x_flat_ready;
    wire whs = in_w_flat_valid && in_w_flat_ready;

    assign in_x_flat_ready = (state == S_LOAD) && (xbeat != XBEATS);
    assign in_w_flat_ready = (state == S_LOAD) && !wgot;
    assign out_valid       = (state == S_RUN);

    integer i;
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_LOAD;
            xbeat <= 4'd0;
            wgot  <= 1'b0;
            ocnt  <= 8'd0;
            fcnt  <= 2'd0;
            rcnt  <= 2'd0;
        end else begin
            case (state)
            S_LOAD: begin
                if (xhs) begin
                    for (i = 0; i < LANES; i = i + 1)
                        sr[xbeat*LANES + i] <= in_x_flat[i*DATA_W +: DATA_W];
                    xbeat <= xbeat + 4'd1;
                end
                if (whs) begin
                    for (i = 0; i < LANES; i = i + 1)
                        wr[i] <= in_w_flat[i*DATA_W +: DATA_W];
                    wgot <= 1'b1;
                end
                if (((xbeat == XBEATS) || (xhs && (xbeat == XBEATS - 4'd1))) &&
                    (wgot || whs)) begin
                    state <= S_RUN;
                    ocnt  <= 8'd0;
                    fcnt  <= 2'd0;
                end
            end
            S_RUN: begin
                if (out_ready) begin
                    // rotate window forward: head becomes x[o+1]
                    for (i = 0; i < IN_LEN-1; i = i + 1)
                        sr[i] <= sr[i+1];
                    sr[IN_LEN-1] <= sr[0];
                    if (ocnt == OUT_POS-1) begin
                        ocnt <= 8'd0;
                        if (fcnt == F-1) begin
                            fcnt  <= 2'd0;
                            state <= S_LOAD;
                            xbeat <= 4'd0;
                            wgot  <= 1'b0;
                        end else begin
                            fcnt  <= fcnt + 2'd1;
                            state <= S_REW;
                            rcnt  <= 2'd0;
                        end
                    end else begin
                        ocnt <= ocnt + 8'd1;
                    end
                end
            end
            default: begin // S_REW: 3 free rotations restore head to x[0]
                for (i = 0; i < IN_LEN-1; i = i + 1)
                    sr[i] <= sr[i+1];
                sr[IN_LEN-1] <= sr[0];
                if (rcnt == 2'd2)
                    state <= S_RUN;
                else
                    rcnt <= rcnt + 2'd1;
            end
            endcase
        end
    end

    // ---------------- datapath (bit-plane decomposition) ----------------
    wire [7:0] x0 = sr[0];
    wire [7:0] x1 = sr[1];
    wire [7:0] x2 = sr[2];
    wire [7:0] x3 = sr[3];

    wire signed [DATA_W-1:0] c0 = wr[fcnt*K + 0];
    wire signed [DATA_W-1:0] c1 = wr[fcnt*K + 1];
    wire signed [DATA_W-1:0] c2 = wr[fcnt*K + 2];
    wire signed [DATA_W-1:0] c3 = wr[fcnt*K + 3];

    // coefficient terms, sign-extended to 11 bits (|S_j| <= 512)
    wire signed [10:0] c0e = c0;
    wire signed [10:0] c1e = c1;
    wire signed [10:0] c2e = c2;
    wire signed [10:0] c3e = c3;

    // S_j = sum_k x_k[j] * c_k  (bit j of x acts as an AND mask)
    wire signed [10:0] Sj [0:7];
    genvar j;
    generate
        for (j = 0; j < 8; j = j + 1) begin : gS
            wire [10:0] e0 = {11{x0[j]}} & c0e;
            wire [10:0] e1 = {11{x1[j]}} & c1e;
            wire [10:0] e2 = {11{x2[j]}} & c2e;
            wire [10:0] e3 = {11{x3[j]}} & c3e;
            assign Sj[j] = e0 + e1 + e2 + e3;
        end
    endgenerate

    // acc = sum_{j=0..6} 2^j * S_j  -  2^7 * S_7   (18-bit signed is exact:
    // sum of |terms| <= 129163 < 2^17)
    wire signed [17:0] T0 = Sj[0];
    wire signed [17:0] T1 = Sj[1] <<< 1;
    wire signed [17:0] T2 = Sj[2] <<< 2;
    wire signed [17:0] T3 = Sj[3] <<< 3;
    wire signed [17:0] T4 = Sj[4] <<< 4;
    wire signed [17:0] T5 = Sj[5] <<< 5;
    wire signed [17:0] T6 = Sj[6] <<< 6;
    wire signed [17:0] T7 = Sj[7] <<< 7;

    wire signed [17:0] acc = T0 + T1 + T2 + T3 + T4 + T5 + T6 - T7;

    assign out_c = {{(ACC_W-18){acc[17]}}, acc};
endmodule
`default_nettype wire
