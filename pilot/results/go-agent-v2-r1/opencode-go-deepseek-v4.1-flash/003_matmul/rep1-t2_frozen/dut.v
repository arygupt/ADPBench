// 003_matmul
// C = A @ B, A: M x K int8 row-major, B: K x N int8 row-major, C: M x N int32
// (exact signed arithmetic, no saturation/rounding).
//
// Streaming valid/ready on both inputs (LANES elements per beat, element j in
// bits [j*DATA_W +: DATA_W]) and on the output (one ACC_W word per beat).
// Two back-to-back transactions without an intervening reset.
//
// Architecture
//   - A arrives K elements per beat = exactly one row of A.  Only two row
//     registers are needed: the row being consumed (a_cur) and the next row
//     (a_pend); the A port is throttled with backpressure accordingly.
//   - B arrives 2*N elements per beat, i.e. beat b holds B[2b][0..N-1] in
//     bytes 0..N-1 and B[2b+1][0..N-1] in bytes N..2N-1.  Beats are stored
//     beat-major and read back transposed, so column j of B is available to
//     the multipliers in a single cycle (only wires in between).
//   - One C element per cycle: K multipliers + balanced adder tree, and an
//     output word is accepted every cycle => 64 cycles per transaction plus
//     the 8-beat input load.
module dut #(
    parameter M      = 8,
    parameter N      = 8,
    parameter K      = 16,
    parameter LANES  = 16,
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

    // ---------------------------------------------------------------- params
    localparam integer VW      = LANES*DATA_W;                 // 128
    localparam integer PW      = 2*DATA_W;                     // 16 (product)
    localparam integer B_BEATS = (K*N + LANES - 1)/LANES;      // 8
    localparam integer LVL     = 4;                            // log2(K) = 4
    localparam integer SW      = PW + LVL;                     // 20 (full sum)

    localparam [1:0] S_LOAD = 2'd0, S_RUN = 2'd1, S_WAIT = 2'd2;

    // ---------------------------------------------------------------- state
    reg [1:0]    state;
    reg [3:0]    b_cnt;      // B beats accepted for this transaction
    reg [2:0]    row, col;   // output word being produced
    reg          a_pend_v;   // a_pend holds the next A row
    reg [VW-1:0] a_cur;      // A row currently being multiplied
    reg [VW-1:0] a_pend;     // A row waiting to become current
    reg [VW-1:0] b_beat [0:B_BEATS-1];

    // ------------------------------------------------------------ handshake
    wire b_ready  = (state == S_LOAD) && (b_cnt < B_BEATS);
    wire b_accept = b_ready && in_b_flat_valid;

    wire a_need   = (state == S_LOAD) || (row < (M-1));
    wire a_ready  = ((state == S_LOAD) || (state == S_RUN) || (state == S_WAIT))
                    && (!a_pend_v) && a_need;
    wire a_accept = a_ready && in_a_flat_valid;

    assign in_b_flat_ready = b_ready;
    assign in_a_flat_ready = a_ready;

    wire b_last      = (b_cnt == B_BEATS-1) && b_accept;
    wire b_load_done = (b_cnt >= B_BEATS) || b_last;

    // ----------------------------------------------------- B beat storage
    genvar gi, gj, gk;
    generate
        for (gi = 0; gi < B_BEATS; gi = gi + 1) begin : g_bcap
            always @(posedge clk) begin
                if (b_accept && (b_cnt == gi))
                    b_beat[gi] <= in_b_flat;
            end
        end
    endgenerate

    // --------------------------------------- transposed view of B (columns)
    // column j, element k sits at b_cols[(j*K + k)*DATA_W +: DATA_W]
    // B[k][j] lives in beat (k/2), byte (((k%2)*N) + j)
    wire [N*K*DATA_W-1:0] b_cols;
    generate
        for (gj = 0; gj < N; gj = gj + 1) begin : g_bcol
            for (gk = 0; gk < K; gk = gk + 1) begin : g_bk
                assign b_cols[(gj*K + gk)*DATA_W +: DATA_W] =
                       b_beat[gk/2][ (((gk % 2) * N) + gj)*DATA_W +: DATA_W ];
            end
        end
    endgenerate

    // selected column for the current output element (pure wire mux)
    wire [K*DATA_W-1:0] b_sel = b_cols[col*(K*DATA_W) +: (K*DATA_W)];

    // ------------------------------------------------------- dot product
    wire signed [PW-1:0] prod [0:K-1];
    generate
        for (gk = 0; gk < K; gk = gk + 1) begin : g_prod
            assign prod[gk] = $signed(a_cur[gk*DATA_W +: DATA_W]) *
                              $signed(b_sel[gk*DATA_W +: DATA_W]);
        end
    endgenerate

    // balanced reduction tree, widths grow by one bit per level
    wire signed [PW:0]   lv1 [0:K/2-1];
    wire signed [PW+1:0] lv2 [0:K/4-1];
    wire signed [PW+2:0] lv3 [0:K/8-1];
    wire signed [PW+3:0] lv4 [0:K/16-1];
    generate
        for (gj = 0; gj < K/2;  gj = gj + 1) assign lv1[gj] = prod[2*gj] + prod[2*gj+1];
        for (gj = 0; gj < K/4;  gj = gj + 1) assign lv2[gj] = lv1[2*gj] + lv1[2*gj+1];
        for (gj = 0; gj < K/8;  gj = gj + 1) assign lv3[gj] = lv2[2*gj] + lv2[2*gj+1];
        for (gj = 0; gj < K/16; gj = gj + 1) assign lv4[gj] = lv3[2*gj] + lv3[2*gj+1];
    endgenerate

    wire signed [SW-1:0] sum_c = lv4[0];

    assign out_c = {{(ACC_W-SW){sum_c[SW-1]}}, sum_c};
    assign out_valid = (state == S_RUN);

    // ---------------------------------------------------------------- FSM
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= S_LOAD;
            b_cnt    <= 4'd0;
            row      <= 3'd0;
            col      <= 3'd0;
            a_pend_v <= 1'b0;
        end else begin
            case (state)
            S_LOAD: begin
                if (a_accept) begin
                    a_pend   <= in_a_flat;
                    a_pend_v <= 1'b1;
                end
                if (b_accept)
                    b_cnt <= b_cnt + 4'd1;
                if (b_load_done && a_pend_v) begin
                    state    <= S_RUN;
                    a_cur    <= a_pend;
                    a_pend_v <= 1'b0;
                    col      <= 3'd0;
                end
            end
            S_RUN: begin
                if (out_ready) begin
                    if (col == N-1) begin
                        if (row == M-1) begin
                            state    <= S_LOAD;
                            b_cnt    <= 4'd0;
                            row      <= 3'd0;
                            col      <= 3'd0;
                            a_pend_v <= 1'b0;
                        end else if (a_pend_v) begin
                            row      <= row + 3'd1;
                            col      <= 3'd0;
                            a_cur    <= a_pend;
                            a_pend_v <= 1'b0;
                        end else begin
                            state <= S_WAIT;
                        end
                    end else begin
                        col <= col + 3'd1;
                    end
                end
                if (a_accept) begin
                    a_pend   <= in_a_flat;
                    a_pend_v <= 1'b1;
                end
            end
            S_WAIT: begin
                if (a_pend_v) begin
                    a_cur    <= a_pend;
                    a_pend_v <= 1'b0;
                    row      <= row + 3'd1;
                    col      <= 3'd0;
                    state    <= S_RUN;
                end else if (a_accept) begin
                    a_cur <= in_a_flat;
                    row   <= row + 3'd1;
                    col   <= 3'd0;
                    state <= S_RUN;
                end
            end
            default: state <= S_LOAD;
            endcase
        end
    end

endmodule
