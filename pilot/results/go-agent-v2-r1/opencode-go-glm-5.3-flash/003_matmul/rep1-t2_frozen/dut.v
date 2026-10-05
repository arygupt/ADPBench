module dut #(
    parameter M = 8,
    parameter N = 8,
    parameter K = 16,
    parameter LANES = 16,
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

localparam A_BEATS = M*K/LANES;   // 8 beats fill A
localparam B_BEATS = K*N/LANES;   // 8 beats fill B
localparam OUTCNT  = M*N;         // 64 outputs per transaction
localparam LASTIDX = OUTCNT - 1;
localparam RPB     = LANES/N;     // B rows per beat (2)

// full A storage
reg [DATA_W-1:0] a_r [0:M*K-1];
// B stored as K cyclic row shift registers (columns rotate each emitted output)
reg [DATA_W-1:0] b_row [0:K*N-1];

// per-transaction input beat counters
reg [3:0] ca, cb;
reg       busy;
reg [5:0] idx;

wire a_room = (ca < A_BEATS);
wire b_room = (cb < B_BEATS);
assign in_a_flat_ready = rst_n & a_room;
assign in_b_flat_ready = rst_n & b_room;

wire a_beat = in_a_flat_valid & in_a_flat_ready;
wire b_beat = in_b_flat_valid & in_b_flat_ready;

wire all_in   = (ca == A_BEATS[3:0]) && (cb == B_BEATS[3:0]);
wire last_out = (idx == LASTIDX[5:0]);
wire out_accept = busy & out_ready;
wire txn_done   = busy & out_accept & last_out;

// current output element position: row i_sel, column j_sel (row-major)
wire [2:0] i_sel = idx[5:3];
wire [2:0] j_sel = idx[2:0];

// select A row i_sel (one element per k)
reg [DATA_W-1:0] a_sel [0:K-1];

integer k, i;
always @* begin
    for (k = 0; k < K; k = k + 1)
        a_sel[k] = {DATA_W{1'b0}};
    for (k = 0; k < K; k = k + 1)
        for (i = 0; i < M; i = i + 1)
            if (i[2:0] == i_sel) a_sel[k] = a_r[i*K + k];
end

// 16 signed 8x8 multipliers
genvar g;
wire signed [15:0] prod [0:K-1];
generate
    for (g = 0; g < K; g = g + 1) begin : MUL
        assign prod[g] = $signed(a_sel[g]) * $signed(b_row[g*N]);
    end
endgenerate

// balanced adder tree, 20-bit signed (max |sum| = 16*16384 = 262144)
wire signed [19:0] s1 [0:7];
wire signed [19:0] s2 [0:3];
wire signed [19:0] s3 [0:1];
wire signed [19:0] ssum;
generate
    for (g = 0; g < 8; g = g + 1) begin : L1
        assign s1[g] = prod[2*g] + prod[2*g+1];
    end
    for (g = 0; g < 4; g = g + 1) begin : L2
        assign s2[g] = s1[2*g] + s1[2*g+1];
    end
    for (g = 0; g < 2; g = g + 1) begin : L3
        assign s3[g] = s2[2*g] + s2[2*g+1];
    end
endgenerate
assign ssum = s3[0] + s3[1];

assign out_c = ssum;

// output handshake: one element per cycle while out_ready, hold when stalled
assign out_valid = busy;

// single driver for all transaction state
integer e, k2, c2;
always @(posedge clk) begin
    if (!rst_n) begin
        busy <= 1'b0;
        idx  <= 6'd0;
        ca   <= 4'd0;
        cb   <= 4'd0;
    end else begin
        // load A beat
        if (a_beat) begin
            for (e = 0; e < LANES; e = e + 1)
                a_r[ca*LANES + e] <= in_a_flat[e*DATA_W +: DATA_W];
            ca <= ca + 4'd1;
        end
        // load B beat into row shift registers (columns 0..N-1)
        if (b_beat) begin
            for (e = 0; e < LANES; e = e + 1)
                b_row[(cb*RPB + e/N)*N + e%N] <= in_b_flat[e*DATA_W +: DATA_W];
            cb <= cb + 4'd1;
        end
        // cyclic left shift of every B row per accepted output;
        // column 0 then always holds the next needed column j = outputs so far
        if (out_accept) begin
            for (k2 = 0; k2 < K; k2 = k2 + 1) begin
                for (c2 = 0; c2 < N-1; c2 = c2 + 1)
                    b_row[k2*N + c2] <= b_row[k2*N + c2 + 1];
                b_row[k2*N + N-1] <= b_row[k2*N + 0];
            end
        end
        if (out_accept)
            idx <= last_out ? 6'd0 : idx + 6'd1;
        if (!busy && all_in) begin
            busy <= 1'b1;
            idx  <= 6'd0;
        end else if (txn_done) begin
            // transaction complete: clear per-transaction state
            busy <= 1'b0;
            idx  <= 6'd0;
            ca   <= 4'd0;
            cb   <= 4'd0;
        end
    end
end

endmodule
