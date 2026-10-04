// 003_matmul: C = A @ B, A (M x K) int8 row-major, B (K x N) int8 row-major,
// C (M x N) int32 exact row-major. Two back-to-back transactions.
//
// B arrives as 8 beats of 2 rows each and is stored transposed (byte k of
// bt_mem[j] = B[k][j]) using enable-decoded writes (no data muxes).
// A is streamed just-in-time: row i is only needed while outputs of row i
// are produced, so A beats are backpressured until B is fully collected and
// then accepted into a 2-deep ping-pong row buffer. This removes 6 row
// registers and turns the A read mux into a 2:1.
// Compute: one output per cycle, 16 signed 8x8 multipliers feeding a
// width-growing exact adder tree; registered output with valid/ready
// backpressure. Deadlock-free: compute of row i waits until row i arrived;
// the A buffer slot for row r is only overwritten once row r-2 started.

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

    localparam BEATS = (K*N)/LANES;   // 8 B beats (also A beats)
    localparam ROWW  = K*DATA_W;      // 128 bits per A row / B column
    localparam OUTS  = M*N;           // 64 outputs
    localparam PW    = 2*DATA_W;      // 16 bits per product

    reg [ROWW-1:0] a_buf  [0:1];      // ping-pong A row buffer
    reg [ROWW-1:0] bt_mem [0:N-1];    // B transposed columns

    reg [3:0] a_cnt;                  // A rows received (0..8)
    reg [3:0] b_cnt;                  // B beats received (0..8)
    reg [5:0] c_cnt;                  // output index 0..63
    reg       running;
    reg       out_valid_r;
    reg signed [ACC_W-1:0] out_c_r;

    wire [2:0] ii = c_cnt[5:3];       // current C row
    wire [2:0] jj = c_cnt[2:0];       // current C column

    // ---------------- handshakes ----------------
    // B is collected first (all of B is needed for any output).
    assign in_b_flat_ready = !running && (b_cnt < BEATS);
    // A rows on demand: keep at most one unread row buffered.
    assign in_a_flat_ready = running && (a_cnt < BEATS) &&
                             (a_cnt < ({1'b0, ii} + 4'd2));

    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire b_fire = in_b_flat_valid && in_b_flat_ready;
    wire b_last = b_fire && (b_cnt == BEATS-1);

    // row ii may be consumed once it has been received
    wire row_ok = (a_cnt > {1'b0, ii});
    wire adv    = row_ok && (!out_valid_r || out_ready);

    // ---------------- dot product datapath ----------------
    wire [ROWW-1:0] a_row = a_buf[ii[0]];
    wire [ROWW-1:0] b_col = bt_mem[jj];

    wire [K*PW-1:0] pvec;
    genvar g;
    generate
        for (g = 0; g < K; g = g + 1) begin : GP
            assign pvec[g*PW +: PW] =
                $signed(a_row[g*DATA_W +: DATA_W]) *
                $signed(b_col[g*DATA_W +: DATA_W]);
        end
    endgenerate

    // width-growing signed adder tree (exact: |sum| <= 2^18)
    wire [(K/2)*(PW+1)-1:0] s1v;  // 8 x 17b
    wire [(K/4)*(PW+2)-1:0] s2v;  // 4 x 18b
    wire [(K/8)*(PW+3)-1:0] s3v;  // 2 x 19b
    wire signed [PW+3:0]    s4;   // 20b
    generate
        for (g = 0; g < K/2; g = g + 1) begin : GS1
            assign s1v[g*(PW+1) +: PW+1] =
                $signed(pvec[(2*g)*PW +: PW]) + $signed(pvec[(2*g+1)*PW +: PW]);
        end
        for (g = 0; g < K/4; g = g + 1) begin : GS2
            assign s2v[g*(PW+2) +: PW+2] =
                $signed(s1v[(2*g)*(PW+1) +: PW+1]) +
                $signed(s1v[(2*g+1)*(PW+1) +: PW+1]);
        end
        for (g = 0; g < K/8; g = g + 1) begin : GS3
            assign s3v[g*(PW+3) +: PW+3] =
                $signed(s2v[(2*g)*(PW+2) +: PW+2]) +
                $signed(s2v[(2*g+1)*(PW+2) +: PW+2]);
        end
    endgenerate
    assign s4 = $signed(s3v[0 +: PW+3]) + $signed(s3v[(PW+3) +: PW+3]);

    wire signed [ACC_W-1:0] dot = {{(ACC_W-PW-4){s4[PW+3]}}, s4};

    // ---------------- control ----------------
    integer m;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_cnt       <= 4'd0;
            b_cnt       <= 4'd0;
            c_cnt       <= 6'd0;
            running     <= 1'b0;
            out_valid_r <= 1'b0;
            out_c_r     <= {ACC_W{1'b0}};
        end else begin
            if (!running) begin
                // COLLECT B (also drains any leftover output)
                if (b_fire) begin
                    for (m = 0; m < N; m = m + 1) begin
                        bt_mem[m][b_cnt*2*DATA_W +: 2*DATA_W] <=
                            { in_b_flat[(N+m)*DATA_W +: DATA_W],
                              in_b_flat[m*DATA_W +: DATA_W] };
                    end
                    b_cnt <= b_cnt + 4'd1;
                end
                if (b_last) begin
                    running <= 1'b1;
                    c_cnt   <= 6'd0;
                end
                if (out_valid_r && out_ready)
                    out_valid_r <= 1'b0;
            end else begin
                // RUN: stream A rows in, emit one output per cycle
                if (a_fire) begin
                    a_buf[a_cnt[0]] <= in_a_flat;
                    a_cnt <= a_cnt + 4'd1;
                end
                if (adv) begin
                    out_c_r     <= dot;
                    out_valid_r <= 1'b1;
                    if (c_cnt == OUTS-1) begin
                        running <= 1'b0;
                        c_cnt   <= 6'd0;
                        a_cnt   <= 4'd0;
                        b_cnt   <= 4'd0;
                    end else begin
                        c_cnt <= c_cnt + 6'd1;
                    end
                end
            end
        end
    end

    assign out_valid = out_valid_r;
    assign out_c     = out_c_r;

endmodule
