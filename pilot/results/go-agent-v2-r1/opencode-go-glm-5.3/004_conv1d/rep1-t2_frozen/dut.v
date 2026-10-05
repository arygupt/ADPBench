// 004_conv1d : exact 1-D valid convolution
//   y[f][o] = sum_k x[o+k]*w[f][k]
//   x : IN_LEN int8, LANES per beat (IN_LEN/LANES beats)
//   w : F*K int8, row-major (1 beat)
//   y : F rows of (IN_LEN-K+1) int32, row-major, one word per beat
//
// x storage: LANES banks of (IN_LEN/LANES) bytes.  Bank b holds x[LANES*m+b]
// at position p, with the invariant  pos p = m = M+p (mod depth).
//  - load push : positions shift down (pos p <= pos p+1), the LAST position
//    takes the incoming beat byte of its bank.  After all beats the array is
//    exactly aligned with M=0.
//  - run rotate: the same position shift, the LAST position takes the old
//    head (ring wrap).  All banks rotate together, whenever the byte latched
//    next for the window is the first of the next group, and once more at the
//    end of each filter row (which returns M to 0).
// Because load and rotate share the byte movement, only the last position of
// each bank needs a second source: one mux per stored bit otherwise.
//
// Window: a K-byte register W = x[o..o+K-1], shifting one byte per accepted
// output word; the incoming byte is read from a bank head (16:1 mux).  At the
// end of a filter row W reloads from position 1 of banks 0..K-1 (which holds
// the m=0 byte at that moment) while the banks rotate back to M=0.

module dut #(
    parameter IN_LEN  = 128,
    parameter F       = 4,
    parameter K       = 4,
    parameter LANES   = 16,
    parameter DATA_W  = 8,
    parameter ACC_W   = 32
)(
    input  wire                     clk,
    input  wire                     rst_n,
    input  wire [LANES*DATA_W-1:0]  in_x_flat,
    input  wire                     in_x_flat_valid,
    output wire                     in_x_flat_ready,
    input  wire [LANES*DATA_W-1:0]  in_w_flat,
    input  wire                     in_w_flat_valid,
    output wire                     in_w_flat_ready,
    output wire                     out_valid,
    input  wire                     out_ready,
    output wire signed [ACC_W-1:0]  out_c
);

    localparam X_BEATS = IN_LEN / LANES;                    // 8
    localparam BD      = IN_LEN / LANES;                   // bank depth 8
    localparam NB      = LANES;                            // banks   16
    localparam W_BYTES = F * K;                            // 16
    localparam W_BEATS = (W_BYTES + LANES - 1) / LANES;     // 1
    localparam OUT_POS = IN_LEN - K + 1;                   // 125

    localparam XC_W = $clog2(X_BEATS + 1);                  // 4
    localparam WC_W = $clog2(W_BEATS + 1);                  // 1
    localparam O_W  = $clog2(OUT_POS);                     // 7
    localparam F_W  = $clog2(F);                           // 2
    localparam LW   = $clog2(LANES);                       // 4
    localparam SUM_W = 2*DATA_W + $clog2(K);               // 18

    localparam ST_LOAD = 1'b0;
    localparam ST_RUN  = 1'b1;

    reg                       state;
    reg [XC_W-1:0]            x_cnt;   // x beats taken this transaction
    reg [WC_W-1:0]            w_cnt;   // w beats taken this transaction
    reg [O_W-1:0]             o;      // output index inside the filter row
    reg [F_W-1:0]             f;      // filter row
    reg [NB*BD*DATA_W-1:0]     bk;     // banks: byte (b,p) at [(b*BD+p)*DW+:DW]
    reg [W_BYTES*DATA_W-1:0]  wreg;   // w row-major
    reg [K*DATA_W-1:0]        W;      // window x[o..o+K-1]

    // ------------------------------------------------------------------
    // input side
    // ------------------------------------------------------------------
    assign in_x_flat_ready = (state == ST_LOAD) && (x_cnt < X_BEATS);
    assign in_w_flat_ready = (state == ST_LOAD) && (w_cnt < W_BEATS);

    wire x_beat = in_x_flat_valid && in_x_flat_ready;
    wire w_beat = in_w_flat_valid && in_w_flat_ready;

    wire [XC_W-1:0] x_cnt_n = x_cnt + x_beat;
    wire [WC_W-1:0] w_cnt_n = w_cnt + w_beat;
    wire load_done  = (x_cnt_n == X_BEATS) && (w_cnt_n == W_BEATS);
    wire x_last     = (x_cnt_n == X_BEATS);

    // ------------------------------------------------------------------
    // output side
    // ------------------------------------------------------------------
    assign out_valid = (state == ST_RUN);
    wire out_beat = out_valid && out_ready;

    wire pass_end = (o == OUT_POS-1);                 // last word of a row
    wire [LW-1:0] jmod = o[LW-1:0] + K;               // (o+K) mod LANES
    // the byte latched at the NEXT word, x[o+K+1], is the first of the next
    // group only when the current latch x[o+K] is the last of its group; and
    // there is no next latch at all when this is the row's last-but-one word
    wire grp_rot  = (jmod == LANES-1) && ((o + 2) < OUT_POS);
    // bank rotation: on the above condition, and once at the end of every row
    wire run_rot  = out_beat && (pass_end || grp_rot);
    // byte movement shared by load push and run rotate
    wire shift_en = x_beat || run_rot;
    // window reload: at the last x beat (pos 1 still holds the m=0 byte), and
    // at the end of a filter row (pos 1 holds m=0 while M wraps to 0)
    wire w_reload = (x_beat && x_last) || (out_beat && pass_end);

    // ------------------------------------------------------------------
    // control
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= ST_LOAD;
            x_cnt <= {XC_W{1'b0}};
            w_cnt <= {WC_W{1'b0}};
            o     <= {O_W{1'b0}};
            f     <= {F_W{1'b0}};
        end else begin
            case (state)
                ST_LOAD: begin
                    if (x_beat) x_cnt <= x_cnt + 1'b1;
                    if (w_beat) w_cnt <= w_cnt + 1'b1;
                    if (load_done) begin
                        state <= ST_RUN;
                        x_cnt <= {XC_W{1'b0}};
                        w_cnt <= {WC_W{1'b0}};
                        o     <= {O_W{1'b0}};
                        f     <= {F_W{1'b0}};
                    end
                end
                ST_RUN: begin
                    if (out_beat) begin
                        if (pass_end) begin
                            o <= {O_W{1'b0}};
                            if (f == F-1) begin
                                state <= ST_LOAD;
                                x_cnt <= {XC_W{1'b0}};
                                w_cnt <= {WC_W{1'b0}};
                                o     <= {O_W{1'b0}};
                                f     <= {F_W{1'b0}};
                            end else begin
                                f <= f + 1'b1;
                            end
                        end else begin
                            o <= o + 1'b1;
                        end
                    end
                end
                default: state <= ST_LOAD;
            endcase
        end
    end

    // ------------------------------------------------------------------
    // banks (no reset: every position is written during the load)
    // ------------------------------------------------------------------
    integer b, p;
    always @(posedge clk) begin
        for (b = 0; b < NB; b = b + 1) begin
            for (p = 0; p < BD-1; p = p + 1) begin
                if (shift_en)
                    bk[(b*BD+p)*DATA_W +: DATA_W] <=
                        bk[(b*BD+p+1)*DATA_W +: DATA_W];
            end
            if (x_beat)
                bk[(b*BD+BD-1)*DATA_W +: DATA_W] <= in_x_flat[b*DATA_W +: DATA_W];
            else if (run_rot)
                bk[(b*BD+BD-1)*DATA_W +: DATA_W] <= bk[b*BD*DATA_W +: DATA_W];
        end
    end

    // ------------------------------------------------------------------
    // w register
    // ------------------------------------------------------------------
    generate
        if (W_BEATS == 1) begin : W_ONE
            always @(posedge clk) begin
                if (w_beat) wreg <= in_w_flat;
            end
        end else begin : W_MANY
            always @(posedge clk) begin
                if (w_beat)
                    wreg <= {in_w_flat,
                             wreg[W_BYTES*DATA_W-1 -: (W_BYTES-LANES)*DATA_W]};
            end
        end
    endgenerate

    // ------------------------------------------------------------------
    // window
    // ------------------------------------------------------------------
    // next byte x[o+K] lives in bank (o+K) mod LANES at its head
    reg [DATA_W-1:0] nxt;
    integer nb;
    always @* begin
        nxt = {DATA_W{1'b0}};
        for (nb = 0; nb < NB; nb = nb + 1) begin
            if (jmod == nb) nxt = bk[nb*BD*DATA_W +: DATA_W];
        end
    end

    integer wi;
    always @(posedge clk) begin
        if (w_reload) begin
            for (wi = 0; wi < K; wi = wi + 1) begin
                W[wi*DATA_W +: DATA_W] <= bk[(wi*BD+1)*DATA_W +: DATA_W];
            end
        end else if (out_beat && !pass_end) begin
            for (wi = 0; wi < K-1; wi = wi + 1) begin
                W[wi*DATA_W +: DATA_W] <= W[(wi+1)*DATA_W +: DATA_W];
            end
            W[(K-1)*DATA_W +: DATA_W] <= nxt;
        end
    end

    // ------------------------------------------------------------------
    // MAC : window W against w row f
    // ------------------------------------------------------------------
    reg [K*DATA_W-1:0] wsel;
    integer fi;
    always @* begin
        wsel = {K*DATA_W{1'b0}};
        for (fi = 0; fi < F; fi = fi + 1) begin
            if (f == fi) wsel = wreg[fi*K*DATA_W +: K*DATA_W];
        end
    end

    genvar ki;
    wire signed [2*DATA_W-1:0] prod [0:K-1];
    generate
        for (ki = 0; ki < K; ki = ki + 1) begin : MULS
            assign prod[ki] = $signed(W[ki*DATA_W +: DATA_W]) *
                              $signed(wsel[ki*DATA_W +: DATA_W]);
        end
    endgenerate

    reg signed [SUM_W-1:0] acc;
    integer ai;
    always @* begin
        acc = {SUM_W{1'b0}};
        for (ai = 0; ai < K; ai = ai + 1) acc = acc + prod[ai];
    end

    assign out_c = {{(ACC_W-SUM_W){acc[SUM_W-1]}}, acc};

endmodule
