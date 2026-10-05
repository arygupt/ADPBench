// 004_conv1d : 1-D valid convolution
//
//   y[f][o] = sum_{k=0}^{K-1} x[o+k] * w[f][k]      o = 0 .. IN_LEN-K
//
// in_x_flat carries IN_LEN int8 samples, LANES per beat (element j of a
// beat occupies bits [j*DATA_W +: DATA_W]).  in_w_flat carries the F*K int8
// weights, row major (w[f][k] at f*K+k).  out_c emits the F*OUT_POS int32
// results row major, one word per beat.  Every port has an independent
// valid/ready handshake; two transactions run back to back without reset.
//
// Organisation: x is buffered as LANES-wide beats with an in-order write
// pointer and an element-granular read pointer that rewinds at the start of
// every filter pass.  A K-deep sliding window over x feeds a K-tap MAC for
// the filter currently being emitted, so one output word is produced per
// cycle and the whole datapath is exact int32 (no saturation, no rounding).
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

  localparam integer OUT_POS = IN_LEN - K + 1;   // 125
  localparam integer N_OUT   = F * OUT_POS;      // 500
  localparam integer XBEATS  = IN_LEN / LANES;   // 8

  localparam integer ECW = $clog2(IN_LEN + 1);   // element counter width
  localparam integer LSW = $clog2(LANES);        // lane  select  width
  localparam integer KW  = $clog2(K + 1);        // window fill   width
  localparam integer OW  = $clog2(OUT_POS + 1);  // output index  width
  localparam integer FW  = $clog2(F + 1);        // filter index  width
  localparam integer NW  = $clog2(N_OUT + 1);    // output count  width

  localparam [1:0] ST_IDLE = 2'd0,
                   ST_FILL = 2'd1,   // preload the K-deep x window
                   ST_RUN  = 2'd2;   // emit results for the current filter

  // ==================================================================
  // declarations
  // ==================================================================
  reg [1:0] state;

  // x buffer : LANES-wide beats, written in order, read element by
  // element; the read pointer rewinds for every filter pass.
  reg [LANES*DATA_W-1:0] x_word [0:XBEATS-1];
  reg [ECW-1:0] x_ecnt;             // elements accepted (0 .. IN_LEN)
  reg [ECW-1:0] x_bcnt;             // beats accepted
  reg [ECW-1:0] eidx;               // next element to fetch
  reg [KW-1:0]  fill_k;             // window slot being preloaded

  // weights
  reg signed [DATA_W-1:0] w_reg [0:F*K-1];
  reg w_loaded;

  // filter / output bookkeeping
  reg  [FW-1:0] f_idx;              // filter being emitted
  reg  [OW-1:0] o_idx;              // output position within filter
  reg  [NW-1:0] emit_cnt;           // results produced
  reg  [NW-1:0] out_cnt;            // results accepted downstream

  // K-deep sliding window : win holds x[o .. o+K-1] for the output that
  // is currently being formed.
  reg signed [DATA_W-1:0] win [0:K-1];

  // output register
  reg               out_valid_r;
  reg signed [ACC_W-1:0] out_c_r;

  // combinational helpers
  reg  [LANES*DATA_W-1:0] x_wrd_q;
  reg signed [DATA_W-1:0] w_sel [0:K-1];
  reg signed [2*DATA_W-1:0] prod;
  reg signed [ACC_W-1:0] acc_c;

  integer ri, gj, fk, gi, si, wi;

  // ==================================================================
  // combinational datapath
  // ==================================================================
  wire x_wen = in_x_flat_valid && in_x_flat_ready;
  wire w_wen = in_w_flat_valid && in_w_flat_ready;

  // x read port : element eidx -> x_el  (explicit mux, constant indices)
  wire [LSW-1:0] e_lane = eidx[LSW-1:0];
  always @* begin
    x_wrd_q = {LANES*DATA_W{1'b0}};
    for (ri = 0; ri < XBEATS; ri = ri + 1)
      if (eidx[ECW-1:LSW] == ri)
        x_wrd_q = x_word[ri];
  end
  wire [DATA_W-1:0] x_el = x_wrd_q[e_lane*DATA_W +: DATA_W];

  // current filter taps : explicit mux over the F filter rows
  always @* begin
    for (gj = 0; gj < K; gj = gj + 1) begin
      w_sel[gj] = {DATA_W{1'b0}};
      for (fk = 0; fk < F; fk = fk + 1)
        if (f_idx == fk)
          w_sel[gj] = w_reg[fk*K + gj];
    end
  end

  // MAC : one exact int32 dot product per cycle
  always @* begin
    acc_c = {ACC_W{1'b0}};
    for (gi = 0; gi < K; gi = gi + 1) begin
      prod  = $signed(win[gi]) * $signed(w_sel[gi]);
      acc_c = acc_c + {{(ACC_W-2*DATA_W){prod[2*DATA_W-1]}}, prod};
    end
  end

  assign out_valid = out_valid_r;
  assign out_c     = out_c_r;

  // stream handshakes (ready withheld while in reset so that no beat can
  // be "accepted" and then discarded)
  assign in_x_flat_ready = rst_n && (x_ecnt != IN_LEN);
  assign in_w_flat_ready = rst_n && !w_loaded;

  wire out_ack  = out_valid_r && out_ready;
  wire txn_done = out_ack && (out_cnt == (N_OUT-1));

  wire last_o = (o_idx == (OUT_POS-1));
  wire win_ok = last_o || (x_ecnt > eidx);          // next x sample loaded?
  wire emit   = (state == ST_RUN) && w_loaded && (emit_cnt != N_OUT) &&
                (!out_valid_r || out_ready) && win_ok;

  // ==================================================================
  // x buffer write process
  // ==================================================================
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      x_ecnt <= {ECW{1'b0}};
      x_bcnt <= {ECW{1'b0}};
    end else begin
      if (txn_done) begin
        x_ecnt <= {ECW{1'b0}};
        x_bcnt <= {ECW{1'b0}};
      end else if (x_wen) begin
        x_word[x_bcnt] <= in_x_flat;
        x_bcnt         <= x_bcnt + 1'b1;
        x_ecnt         <= x_ecnt + LANES;
      end
    end
  end

  // ==================================================================
  // main sequential process
  // ==================================================================
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state       <= ST_IDLE;
      w_loaded    <= 1'b0;
      eidx        <= {ECW{1'b0}};
      fill_k      <= {KW{1'b0}};
      o_idx       <= {OW{1'b0}};
      f_idx       <= {FW{1'b0}};
      emit_cnt    <= {NW{1'b0}};
      out_cnt     <= {NW{1'b0}};
      out_valid_r <= 1'b0;
      out_c_r     <= {ACC_W{1'b0}};
      for (si = 0; si < K; si = si + 1)
        win[si] <= {DATA_W{1'b0}};
    end else begin
      // ---- capture weights -------------------------------------------
      if (w_wen) begin
        for (wi = 0; wi < F*K; wi = wi + 1)
          w_reg[wi] <= in_w_flat[wi*DATA_W +: DATA_W];
        w_loaded <= 1'b1;
      end

      // ---- downstream handshake -------------------------------------
      if (out_ack) begin
        out_cnt     <= out_cnt + 1'b1;
        out_valid_r <= 1'b0;
      end

      // ---- transaction boundary --------------------------------------
      if (txn_done) begin
        state       <= ST_IDLE;
        w_loaded    <= 1'b0;
        eidx        <= {ECW{1'b0}};
        fill_k      <= {KW{1'b0}};
        o_idx       <= {OW{1'b0}};
        f_idx       <= {FW{1'b0}};
        emit_cnt    <= {NW{1'b0}};
        out_cnt     <= {NW{1'b0}};
        out_valid_r <= 1'b0;
      end

      // ---- emit one result -------------------------------------------
      if (emit) begin
        out_c_r     <= acc_c;
        out_valid_r <= 1'b1;
        emit_cnt    <= emit_cnt + 1'b1;
        if (last_o) begin
          o_idx <= {OW{1'b0}};
          if (f_idx == (F-1)) begin
            // last result of the transaction: hold it until accepted
          end else begin
            f_idx  <= f_idx + 1'b1;
            eidx   <= {ECW{1'b0}};
            fill_k <= {KW{1'b0}};
            state  <= ST_FILL;
          end
        end else begin
          o_idx <= o_idx + 1'b1;
          for (si = 0; si < K-1; si = si + 1)
            win[si] <= win[si+1];
          win[K-1] <= x_el;
          eidx     <= eidx + 1'b1;
        end
      end

      // ---- FSM --------------------------------------------------------
      case (state)
        ST_IDLE: begin
          if (x_ecnt != 0) begin        // first x beat -> preload window
            state  <= ST_FILL;
            eidx   <= {ECW{1'b0}};
            fill_k <= {KW{1'b0}};
          end
        end
        ST_FILL: begin
          win[fill_k] <= x_el;
          eidx        <= eidx + 1'b1;
          if (fill_k == (K-1))
            state <= ST_RUN;
          else
            fill_k <= fill_k + 1'b1;
        end
        default: begin
        end
      endcase
    end
  end

endmodule
