// -----------------------------------------------------------------------------
// 004_conv1d
//   y[f][o] = sum_{k=0..K-1} x[o+k] * w[f][k],  o = 0..OUT_POS-1
//
// Implementation notes
// --------------------
// * All IN_LEN samples live in a packed circular byte register ("ring").
//   The ring is only ever moved in LANES-byte steps: during the input phase
//   each accepted beat is shifted in at the top, during the compute phase the
//   ring rotates by LANES bytes ("burst") whenever the read window runs out.
//   Both moves use the *same* wiring for every bit except the top group, so
//   the ring costs one mux per bit plus one mux per top-group bit.
// * Bursts are enough because the tap block only needs LANES bytes of head
//   room.  `p0` (0..LANES-1) is the byte offset of the tap block inside the
//   ring; it advances by 1 per emitted word and by K at a row wrap (row f+1
//   restarts at x[0] while the ring keeps turning).  Whenever p0 would leave
//   [0..LANES-1] a burst consumes the slack, so a whole row of OUT_POS words
//   advances the ring by exactly IN_LEN bytes and p0 returns to its start.
// * The K taps come out of the fixed ring window through a small barrel
//   shifter (offset p0); the weights through another (offset fcnt*K).
// * One word per cycle: K 8x8 signed multipliers plus adder chain into the
//   signed ACC_W output register.  Backpressure freezes ring, window and
//   position/row counters.
// -----------------------------------------------------------------------------
module dut #(
    parameter IN_LEN = 128,
    parameter F      = 4,
    parameter K      = 4,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
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

    localparam integer OUT_POS = IN_LEN - K + 1;
    localparam integer XB      = (IN_LEN + LANES - 1) / LANES;   // x beats
    localparam integer WB      = (F * K + LANES - 1) / LANES;    // w beats
    localparam integer SUMW    = 2 * DATA_W + ((K > 1) ? $clog2(K) : 0);
    localparam integer EXT     = ACC_W - SUMW;
    localparam integer FW      = (F > 1) ? $clog2(F) : 1;
    localparam integer CW      = 8;
    localparam integer BW      = 5;      // window-offset counter width
    localparam integer GW      = 2;      // group-select bits of the window
    localparam integer RW      = 2;      // within-group offset bits

    localparam [BW:0] WMAX  = LANES - 1;                    // max window offset
    localparam [BW:0] WSTEP = LANES;                        // burst size
    localparam [BW:0] WINCR = K;                            // row-wrap advance

    localparam S_LOAD = 1'b0;
    localparam S_RUN  = 1'b1;

    reg                       state;
    reg  [CW-1:0]             xcnt;
    reg  [CW-1:0]             wcnt;
    reg  [CW-1:0]             ocnt;
    reg  [FW-1:0]             fcnt;
    reg  [IN_LEN*DATA_W-1:0]  xr;      // circular sample storage
    reg  [F*K*DATA_W-1:0]     wr;      // filter taps, row major
    reg  signed [ACC_W-1:0]   out_q;
    reg                       out_v;
    reg  [BW-1:0]             p0;      // window offset of the tap block

    // ---------------------------------------------------------------- handshake
    wire xfer_x   = in_x_flat_valid & in_x_flat_ready;
    wire xfer_w   = in_w_flat_valid & in_w_flat_ready;
    wire fire     = out_v & out_ready;
    wire advance  = (state == S_RUN) & (fire | ~out_v);

    assign in_x_flat_ready = (state == S_LOAD) & (xcnt < XB);
    assign in_w_flat_ready = (state == S_LOAD) & (wcnt < WB);
    assign out_valid       = out_v;
    assign out_c           = out_q;

    wire x_done = (xcnt >= XB);
    wire w_done = (wcnt >= WB);
    wire x_last = xfer_x & (xcnt == XB - 1);
    wire w_last = xfer_w & (wcnt == WB - 1);
    wire load_done = (x_done | x_last) & (w_done | w_last);

    // ------------------------------------------------------ window bookkeeping
    wire         last_row = (ocnt == OUT_POS - 1);
    wire [BW:0]  p0inc    = last_row ? WINCR : {{BW{1'b0}}, 1'b1};
    wire [BW:0]  p0n      = {1'b0, p0} + p0inc;
    wire         burst    = (state == S_RUN) & advance & (p0n > WMAX);

    // ------------------------------------------------------------- ring move
    wire shift_en = (state == S_LOAD) ? xfer_x : burst;
    wire [LANES*DATA_W-1:0] top_in =
        (state == S_LOAD) ? in_x_flat : xr[LANES*DATA_W-1:0];

    // -------------------------------------------------- tap barrel shifter
    // K consecutive bytes at offset p0 (0..LANES-1) inside the ring window.
    wire [8*DATA_W-1:0] w8;                 // bytes [4*p0[GW+1:2] .. +7]
    genvar gj;
    generate
        for (gj = 0; gj < 8; gj = gj + 1) begin : L1
            assign w8[gj*DATA_W +: DATA_W] =
                (p0[GW+1:2] == 2'd0) ? xr[(0  + gj)*DATA_W +: DATA_W] :
                (p0[GW+1:2] == 2'd1) ? xr[(4  + gj)*DATA_W +: DATA_W] :
                (p0[GW+1:2] == 2'd2) ? xr[(8  + gj)*DATA_W +: DATA_W] :
                                       xr[(12 + gj)*DATA_W +: DATA_W];
        end
    endgenerate

    wire [DATA_W-1:0] v0 = p0[0] ? w8[1*DATA_W +: DATA_W] : w8[0*DATA_W +: DATA_W];
    wire [DATA_W-1:0] v1 = p0[0] ? w8[2*DATA_W +: DATA_W] : w8[1*DATA_W +: DATA_W];
    wire [DATA_W-1:0] v2 = p0[0] ? w8[3*DATA_W +: DATA_W] : w8[2*DATA_W +: DATA_W];
    wire [DATA_W-1:0] v3 = p0[0] ? w8[4*DATA_W +: DATA_W] : w8[3*DATA_W +: DATA_W];
    wire [DATA_W-1:0] v4 = p0[0] ? w8[5*DATA_W +: DATA_W] : w8[4*DATA_W +: DATA_W];
    wire [DATA_W-1:0] v5 = p0[0] ? w8[6*DATA_W +: DATA_W] : w8[5*DATA_W +: DATA_W];

    // byte k of tapv = x[o+k]
    wire [K*DATA_W-1:0] tapv = {(p0[1] ? v5 : v3), (p0[1] ? v4 : v2),
                                (p0[1] ? v3 : v1), (p0[1] ? v2 : v0)};

    // ---------------------------------------------- weight (row) selection
    wire [K*DATA_W-1:0] wselv = wr >> (fcnt * (K*DATA_W));

    // ---------------------------------------------------------- MAC datapath
    wire signed [2*DATA_W-1:0] prod [0:K-1];
    genvar gk;
    generate
        for (gk = 0; gk < K; gk = gk + 1) begin : MAC
            assign prod[gk] = $signed(tapv[gk*DATA_W +: DATA_W]) *
                              $signed(wselv[gk*DATA_W +: DATA_W]);
        end
    endgenerate

    reg signed [SUMW-1:0] psum;
    integer sk;
    always @* begin
        psum = {SUMW{1'b0}};
        for (sk = 0; sk < K; sk = sk + 1) begin
            psum = psum + prod[sk];
        end
    end

    wire signed [ACC_W-1:0] psum_ext = {{EXT{psum[SUMW-1]}}, psum};

    // ---------------------------------------------------------------- control
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_LOAD;
            xcnt  <= {CW{1'b0}};
            wcnt  <= {CW{1'b0}};
            ocnt  <= {CW{1'b0}};
            fcnt  <= {FW{1'b0}};
            xr    <= {IN_LEN*DATA_W{1'b0}};
            wr    <= {F*K*DATA_W{1'b0}};
            out_q <= {ACC_W{1'b0}};
            out_v <= 1'b0;
            p0    <= {BW{1'b0}};
        end else begin
            case (state)
                S_LOAD: begin
                    if (fire) out_v <= 1'b0;

                    if (xfer_x) xcnt <= xcnt + 1'b1;

                    if (xfer_w) begin
                        wr   <= (wr >> (LANES*DATA_W)) |
                                (in_w_flat << (F*K*DATA_W - LANES*DATA_W));
                        wcnt <= wcnt + 1'b1;
                    end

                    if (load_done) begin
                        state <= S_RUN;
                        ocnt  <= {CW{1'b0}};
                        fcnt  <= {FW{1'b0}};
                        p0    <= {BW{1'b0}};
                    end
                end

                S_RUN: begin
                    if (advance) begin
                        out_q <= psum_ext;
                        out_v <= 1'b1;
                        p0    <= burst ? (p0n - WSTEP) : p0n[BW-1:0];
                        if (ocnt == OUT_POS - 1) begin
                            ocnt <= {CW{1'b0}};
                            if (fcnt == F - 1) begin
                                fcnt  <= {FW{1'b0}};
                                state <= S_LOAD;
                                xcnt  <= {CW{1'b0}};
                                wcnt  <= {CW{1'b0}};
                            end else begin
                                fcnt <= fcnt + 1'b1;
                            end
                        end else begin
                            ocnt <= ocnt + 1'b1;
                        end
                    end
                end

                default: state <= S_LOAD;
            endcase

            // one LANES-byte ring step (beat load, or compute burst)
            if (shift_en) begin
                xr <= {top_in, xr[IN_LEN*DATA_W-1 : LANES*DATA_W]};
            end
        end
    end

endmodule
