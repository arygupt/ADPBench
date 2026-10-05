//======================================================================
// dut : C = A @ B   (A: M x K row-major, B: K x N row-major)
//
// int8 inputs, exact int32 output, two back-to-back transactions with
// independent per-port valid/ready handshakes.
//
// Organisation (here LANES == K, so one input beat is exactly one A row):
//   * A rows are consumed strictly in row order and only the row being
//     output is live, so each A beat lands directly in a single current
//     row register: no A storage array and no A read multiplexer.
//   * Every C word needs every B row, so B is kept in beat slots (one-hot
//     written) and fetched as K byte columns through the column mux.
//   * K parallel signed multipliers form the K products of the current C
//     element in one cycle; a combinational K:1 adder tree reduces them
//     exactly, so one output word is emitted every cycle - the output
//     bandwidth limit of the interface.
//   * An A beat is taken as soon as its row is no longer needed by a word
//     that is still being presented; while the output waits for a row it
//     drops out_valid and holds the request open, so the stream
//     self-aligns with no deadlock.  Per-transaction state is cleared
//     when the last output word is accepted.
//======================================================================
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

    // ------------------------------------------------------------------
    // Derived constants
    // ------------------------------------------------------------------
    localparam A_LEN   = M * K;                    // 128 A elements
    localparam B_LEN   = K * N;                    // 128 B elements
    localparam C_LEN   = M * N;                    // 64  C words
    localparam A_BEATS = A_LEN / LANES;            // 8  (== M rows)
    localparam B_BEATS = B_LEN / LANES;            // 8

    localparam PW    = 2 * DATA_W;                 // 16  product width
    localparam SUM_W = 2 * DATA_W + $clog2(K);     // 20  exact K-term sum

    localparam ROWW = $clog2(M);                   // 3
    localparam COLW = $clog2(N);                   // 3
    localparam AIXW = $clog2(A_BEATS + 1);         // 4

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------
    reg  [B_BEATS*LANES*DATA_W-1:0] b_store;       // flat row-major B
    reg  [B_BEATS-1:0]              b_oh;          // next B slot, one-hot

    reg  [K*DATA_W-1:0] a_cur;                     // A row being output
    reg  [AIXW-1:0]     a_aidx;                    // A rows taken (0..M)

    reg  [ROWW-1:0]     row_ctr;                   // output row
    reg  [COLW-1:0]     col_ctr;                   // output column

    wire b_full = (b_oh == {B_BEATS{1'b0}});       // all B rows resident
    wire a_done = (a_aidx == A_BEATS);             // all A rows taken

    // a_cur holds row row_ctr, so the current word may be presented
    wire row_ready = (a_aidx != {AIXW{1'b0}}) & (row_ctr == a_aidx - 1'b1);
    assign out_valid = b_full & row_ready;

    wire o_fire = out_valid & out_ready;
    wire o_last = o_fire & (row_ctr == M-1) & (col_ctr == N-1);

    // A beat a_aidx may be taken when row a_aidx-1 has been fully
    // accepted (or nothing is held yet); taking it early is never
    // possible because a live row would be clobbered.
    wire a_room = (a_aidx == {AIXW{1'b0}})
                | (row_ctr == a_aidx)
                | (o_fire & (row_ctr == a_aidx - 1'b1) & (col_ctr == N-1));

    assign in_a_flat_ready = rst_n & ~a_done & a_room;
    assign in_b_flat_ready = rst_n & ~b_full;

    wire a_fire = in_a_flat_valid & in_a_flat_ready;
    wire b_fire = in_b_flat_valid & in_b_flat_ready;

    // ------------------------------------------------------------------
    // Control: slot fill for B, row hand-off for A, output position
    // ------------------------------------------------------------------
    integer si;
    always @(posedge clk) begin
        if (!rst_n) begin
            a_cur   <= {K*DATA_W{1'b0}};
            a_aidx  <= {AIXW{1'b0}};
            b_oh    <= {{(B_BEATS-1){1'b0}}, 1'b1};
            row_ctr <= {ROWW{1'b0}};
            col_ctr <= {COLW{1'b0}};
        end else begin
            // B slot the pointer marks is refreshed every cycle; a slot
            // is only read after its beat's handshake committed the real
            // data, so transient bus contents never survive.
            for (si = 0; si < B_BEATS; si = si + 1)
                if (b_oh[si]) b_store[si*LANES*DATA_W +: LANES*DATA_W] <= in_b_flat;

            if (o_last) begin
                // last output word accepted: clear per-transaction state
                a_aidx  <= {AIXW{1'b0}};
                b_oh    <= {{(B_BEATS-1){1'b0}}, 1'b1};
                row_ctr <= {ROWW{1'b0}};
                col_ctr <= {COLW{1'b0}};
            end else begin
                if (a_fire) begin
                    a_cur  <= in_a_flat;          // beat == one A row
                    a_aidx <= a_aidx + 1'b1;
                end
                if (b_fire) b_oh <= {b_oh[B_BEATS-2:0], 1'b0};
                if (o_fire) begin
                    if (col_ctr == N-1) begin
                        col_ctr <= {COLW{1'b0}};
                        row_ctr <= row_ctr + 1'b1;
                    end else begin
                        col_ctr <= col_ctr + 1'b1;
                    end
                end
            end
        end
    end

    // ------------------------------------------------------------------
    // K parallel multipliers for C[row_ctr][col_ctr]
    // ------------------------------------------------------------------
    wire [K*DATA_W-1:0] b_col;
    wire [K*PW-1:0]     prod;

    genvar gk;
    generate
        for (gk = 0; gk < K; gk = gk + 1) begin : g_op
            // byte gk of the fetch holds B[gk][col_ctr]
            assign b_col[gk*DATA_W +: DATA_W] =
                   b_store[(gk*N + col_ctr)*DATA_W +: DATA_W];

            wire signed [DATA_W-1:0] av = $signed(a_cur[gk*DATA_W +: DATA_W]);
            wire signed [DATA_W-1:0] bv = $signed(b_col[gk*DATA_W +: DATA_W]);
            assign prod[gk*PW +: PW] = av * bv;      // exact int16 product
        end
    endgenerate

    // ------------------------------------------------------------------
    // Exact K-term sum (int32 result, no saturation / rounding)
    // ------------------------------------------------------------------
    reg signed [SUM_W-1:0] acc_c;
    integer ti;
    always @* begin
        acc_c = {SUM_W{1'b0}};
        for (ti = 0; ti < K; ti = ti + 1) begin
            acc_c = acc_c + $signed(prod[ti*PW +: PW]);
        end
    end

    assign out_c = acc_c;                           // sign-extends to ACC_W

endmodule
