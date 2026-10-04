// GEMV: y = A @ x, A is 16x64 int8 row-major, x is 64 int8, y is 16 int32.
// Beats of LANES=16 elements. Double-buffered x banks and double-buffered
// accumulator banks so transaction t+1 computes while t drains. One 16-lane
// signed 8x8 multiply array + adder tree per cycle; first segment of a row
// loads the accumulator (later segments add), so no clear pass is needed.
// Minimum exact bit widths: product 16b, tree up to 20b, accumulator 22b
// (max |sum| = 64*16384 = 2^20), sign-extended to 32b at the output.

module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam integer SEGS  = COLS / LANES;      // 4 x beats per transaction
    localparam integer ABEAT = ROWS * SEGS;       // 64 a beats per transaction
    localparam integer AW    = 22;                // exact accumulator width

    // ---------------- state ----------------
    reg [COLS*DATA_W-1:0]  xq   [0:1];            // x banks, flat
    reg signed [AW-1:0]    acc  [0:1][0:ROWS-1];  // accumulator banks

    reg [2:0]              xcnt [0:1];            // accepted x beats per bank (0..4)
    reg                    xbank;                 // bank being filled by x port
    reg [5:0]              abeat;                 // a beat index within transaction (0..63)
    reg                    abank;                 // bank currently computing
    reg [3:0]              ocnt;                  // output word index (0..15)
    reg                    obank;                 // bank being drained to output
    reg [1:0]              pend;                  // per-bank "result pending" flag

    wire x_acc = in_x_flat_valid & in_x_flat_ready;
    wire a_acc = in_a_flat_valid & in_a_flat_ready;
    wire o_acc = out_valid & out_ready;

    assign in_x_flat_ready = (xcnt[xbank] != 3'd4);
    assign in_a_flat_ready = (xcnt[abank] == 3'd4) && !pend[abank];
    assign out_valid       = pend[obank];

    wire signed [AW-1:0] oword = acc[obank][ocnt];
    assign out_c = {{ACC_W-AW{oword[AW-1]}}, oword};

    // ---------------- datapath ----------------
    wire [1:0]  aseg = abeat[1:0];              // x segment for this a beat
    wire [3:0]  arow = abeat[5:2];              // row for this a beat
    wire [1:0]  xseg = xcnt[xbank][1:0];       // segment being written

    wire signed [DATA_W-1:0] av [0:LANES-1];
    wire signed [DATA_W-1:0] xv [0:LANES-1];
    wire signed [2*DATA_W-1:0] p0 [0:LANES-1];

    genvar k;
    generate
        for (k = 0; k < LANES; k = k + 1) begin : g_lane
            assign av[k] = in_a_flat[k*DATA_W +: DATA_W];
            assign xv[k] = xq[abank][(aseg*LANES + k)*DATA_W +: DATA_W];
            assign p0[k] = av[k] * xv[k];
        end
    endgenerate

    // adder tree: 16 -> 8 -> 4 -> 2 -> 1, minimum exact widths
    wire signed [2*DATA_W:0]        p1 [0:LANES/2-1];
    wire signed [2*DATA_W+1:0]      p2 [0:LANES/4-1];
    wire signed [2*DATA_W+2:0]      p3 [0:LANES/8-1];
    wire signed [2*DATA_W+3:0]      p4;

    generate
        for (k = 0; k < LANES/2; k = k + 1) begin : g_t1
            assign p1[k] = p0[2*k] + p0[2*k+1];
        end
        for (k = 0; k < LANES/4; k = k + 1) begin : g_t2
            assign p2[k] = p1[2*k] + p1[2*k+1];
        end
        for (k = 0; k < LANES/8; k = k + 1) begin : g_t3
            assign p3[k] = p2[2*k] + p2[2*k+1];
        end
    endgenerate
    assign p4 = p3[0] + p3[1];

    wire signed [AW-1:0] p4ext = p4;   // sign-extend 20 -> 22

    // ---------------- sequential ----------------
    always @(posedge clk) begin
        if (!rst_n) begin
            xbank <= 1'b0;
            abeat <= 6'd0;
            abank <= 1'b0;
            ocnt  <= 4'd0;
            obank <= 1'b0;
            pend  <= 2'd0;
            xcnt[0] <= 3'd0;
            xcnt[1] <= 3'd0;
        end else begin
            // x input stream
            if (x_acc) begin
                xq[xbank][xseg*LANES*DATA_W +: LANES*DATA_W] <= in_x_flat;
                if (xcnt[xbank] == 3'd3) begin
                    xcnt[xbank] <= 3'd4;
                    xbank       <= ~xbank;
                end else begin
                    xcnt[xbank] <= xcnt[xbank] + 3'd1;
                end
            end

            // a input stream + accumulate
            if (a_acc) begin
                if (aseg == 2'd0)
                    acc[abank][arow] <= p4ext;
                else
                    acc[abank][arow] <= acc[abank][arow] + p4ext;

                if (abeat == ABEAT-1) begin
                    abeat         <= 6'd0;
                    pend[abank]   <= 1'b1;
                    xcnt[abank]   <= 3'd0;   // consumed this bank's x
                    abank         <= ~abank;
                end else begin
                    abeat <= abeat + 6'd1;
                end
            end

            // output drain
            if (o_acc) begin
                if (ocnt == ROWS-1) begin
                    pend[obank] <= 1'b0;
                    obank       <= ~obank;
                    ocnt        <= 4'd0;
                end else begin
                    ocnt <= ocnt + 4'd1;
                end
            end
        end
    end

endmodule
