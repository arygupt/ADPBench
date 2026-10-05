// C = A @ B : A (8x16 int8 row-major), B (16x8 int8 row-major),
// C (8x8 int32 row-major) bit-exact.
//
// Micro-architecture:
//  * Capture A (8 beats) and B (8 beats) into flop memories, parallel load.
//  * Compute 2 rows of C per group with 16 single-cycle MAC lanes, sweeping
//    k = 0..15.  Accumulators are 20-bit (exact: |C| <= 2^18); sign extended
//    at the output.  Each acc set is zeroed when its stream finishes, so the
//    sweep needs no first-term select mux.  Group 0's k=0 term is folded
//    into the last capture cycle.
//  * Two acc sets ping-pong: group q streams its 16 words while group q+1
//    computes (seamless: stream start same cycle as group completion).
//    Compute stalls only if it would write a set that is being streamed or
//    is still pending a stream (backpressure safe).
//  * Independent input handshakes; output backpressure honored; transaction
//    end clears state, supporting back-to-back transactions without reset.

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

    localparam AW = 20;   // accumulator width (exact for |C| <= 262144)

    // ---------------------------------------------------------------- control
    reg        is_capt;         // 1 = input capture phase
    reg [3:0]  cnt_a, cnt_b;    // beats accepted (0..8)
    reg [2:0]  gp;              // group computing now (4 = compute done)
    reg [3:0]  k;               // k sweep 0..15
    reg        cset;            // acc set being computed
    reg        busy;            // streaming active
    reg        sset;            // acc set being streamed
    reg [3:0]  w;               // stream word index 0..15
    reg [3:0]  pend;            // bit g: group g computed, awaiting stream

    // ---------------------------------------------------------------- memories
    reg [7:0]           Am [0:127];   // A flat: Am[row*16 + kcol]
    reg [7:0]           Bm [0:127];   // B flat: Bm[krow*8 + j]
    reg signed [AW-1:0] acc [0:31];   // {set, lane}

    integer ii, cc;

    // ---- A operand read: stage 1 row-pair select, stage 2 k select ----
    reg [7:0] E [0:15];   // E[c] = A[2gp][c]
    reg [7:0] O [0:15];   // O[c] = A[2gp+1][c]
    reg signed [7:0] av0, av1, a_t, b_t;
    reg signed [15:0] p_t;
    reg signed [AW-1:0] PR [0:15];
    reg signed [AW-1:0] sout;

    always @(*) begin
        for (cc = 0; cc < 16; cc = cc + 1) begin
            E[cc] = gp[1] ? (gp[0] ? Am[96+cc]  : Am[64+cc])
                          : (gp[0] ? Am[32+cc]  : Am[cc]);
            O[cc] = gp[1] ? (gp[0] ? Am[112+cc] : Am[80+cc])
                          : (gp[0] ? Am[48+cc]  : Am[16+cc]);
        end
        av0 = $signed(E[k]);
        av1 = $signed(O[k]);
        for (ii = 0; ii < 16; ii = ii + 1) begin
            a_t = ii[3] ? av1 : av0;
            b_t = $signed(Bm[{k, ii[2:0]}]);
            p_t = a_t * b_t;
            PR[ii[3:0]] = {{(AW-16){p_t[15]}}, p_t};
        end
        sout = acc[{sset, w}];
    end

    assign out_c = {{(ACC_W-AW){sout[AW-1]}}, sout};

    // ---------------------------------------------------------------- status
    wire [2:0] g3       = gp[2:0];
    wire       capt_done = (cnt_a == 4'd8) && (cnt_b == 4'd8);
    wire [3:0] pmask    = cset ? 4'b1010 : 4'b0101;
    wire       stall_p  = |(pend & pmask);
    wire       stall_b  = busy && (sset == cset);
    wire       stall    = stall_p || stall_b;
    wire       comp_act = (gp != 3'd4) && (!is_capt || capt_done) && !stall;
    wire       k_last   = (k == 4'd15);

    wire       str_acc  = busy && out_ready;
    wire       end_now  = str_acc && (w == 4'd15);
    wire       idle_now = (!busy) || end_now;

    wire [3:0] pnd_n = pend | ((comp_act && k_last) ? (4'b1 << g3[1:0]) : 4'b0);
    wire [1:0] pi    = pnd_n[0] ? 2'd0 : pnd_n[1] ? 2'd1 : pnd_n[2] ? 2'd2 : 2'd3;

    wire txn_end = end_now && (gp == 3'd4) && (pnd_n == 4'd0);

    assign out_valid       = busy;
    assign in_a_flat_ready = is_capt && !cnt_a[3] && rst_n;
    assign in_b_flat_ready = is_capt && !cnt_b[3] && rst_n;

    // ---------------------------------------------------------------- datapath
    always @(posedge clk) begin
        if (rst_n && is_capt && !cnt_a[3] && in_a_flat_valid)
            for (ii = 0; ii < 16; ii = ii + 1)
                Am[{cnt_a, ii[3:0]}] <= in_a_flat[ii*8 +: 8];
    end

    always @(posedge clk) begin
        if (rst_n && is_capt && !cnt_b[3] && in_b_flat_valid)
            for (ii = 0; ii < 16; ii = ii + 1)
                Bm[{cnt_b, ii[3:0]}] <= in_b_flat[ii*8 +: 8];
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            for (ii = 0; ii < 32; ii = ii + 1) acc[ii] <= {AW{1'b0}};
        end else begin
            if (end_now)                              // zero the set just streamed
                for (ii = 0; ii < 16; ii = ii + 1)
                    acc[{sset, ii[3:0]}] <= {AW{1'b0}};
            if (comp_act)                             // accumulate
                for (ii = 0; ii < 16; ii = ii + 1)
                    acc[{cset, ii[3:0]}] <= acc[{cset, ii[3:0]}] + PR[ii[3:0]];
        end
    end

    // ---------------------------------------------------------------- FSM
    always @(posedge clk) begin
        if (!rst_n) begin
            is_capt <= 1'b1; cnt_a <= 4'd0; cnt_b <= 4'd0;
            gp <= 3'd0; k <= 4'd0; cset <= 1'b0;
            busy <= 1'b0; sset <= 1'b0; w <= 4'd0; pend <= 4'd0;
        end else begin
            if (is_capt && in_a_flat_ready && in_a_flat_valid) cnt_a <= cnt_a + 4'd1;
            if (is_capt && in_b_flat_ready && in_b_flat_valid) cnt_b <= cnt_b + 4'd1;

            // phase exit (compute progress below handles the first term)
            if (is_capt && capt_done) is_capt <= 1'b0;

            // compute progress
            if (comp_act) begin
                if (k_last) begin
                    pend[g3[1:0]] <= 1'b1;
                    k    <= 4'd0;
                    cset <= ~cset;
                    gp   <= (gp == 3'd3) ? 3'd4 : gp + 3'd1;
                end else begin
                    k <= k + 4'd1;
                end
            end

            // stream advance
            if (str_acc) begin
                if (end_now) begin
                    busy <= 1'b0;
                    w    <= 4'd0;
                end else begin
                    w <= w + 4'd1;
                end
            end

            // start next pending stream when port idle (same cycle as end/complete)
            if (idle_now && (|pnd_n) && !txn_end) begin
                busy       <= 1'b1;
                sset       <= pi[0];
                w          <= 4'd0;
                pend[pi]   <= 1'b0;
            end

            // transaction finished
            if (txn_end) begin
                is_capt <= 1'b1;
                cnt_a  <= 4'd0;
                cnt_b  <= 4'd0;
                gp     <= 3'd0;
                k      <= 4'd0;
                cset   <= 1'b0;
                busy   <= 1'b0;
                sset   <= 1'b0;
                w      <= 4'd0;
                pend   <= 4'd0;
            end
        end
    end

endmodule
