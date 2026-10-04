// ---------------------------------------------------------------------------
// 002_gemv : y = A @ x
//   A : ROWS x COLS int8, row major, streamed LANES elements per beat
//       (ROWS*COLS)/LANES beats per transaction (4 beats per row here).
//   x : COLS int8, streamed LANES elements per beat (COLS/LANES beats).
//   y : ROWS int32, one word per row.
//
// Architecture: one A beat is consumed per clock; all LANES multipliers run
// in parallel straight off the input port, so no A buffering is needed.  The
// x groups are held in registers; the group needed by the current A beat is
// selected, or taken directly from the x input port when that group's beat is
// accepted in the same cycle.  A row accumulator collects the LANES-wide dot
// products; the last beat of a row finishes it into the output register.
//
// Handshakes: the two input streams are independent.  An A beat is accepted
// only when its x group is available and no finished row is stuck waiting for
// out_ready.  After the last A/x beat of a transaction no further input is
// accepted until the last output word has been accepted; then all
// per-transaction state is cleared.
// ---------------------------------------------------------------------------
module dut #(
    parameter ROWS   = 16,
    parameter COLS   = 64,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                     clk,
    input  wire                     rst_n,
    input  wire [LANES*DATA_W-1:0]  in_a_flat,
    input  wire                     in_a_flat_valid,
    output wire                     in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0]  in_x_flat,
    input  wire                     in_x_flat_valid,
    output wire                     in_x_flat_ready,
    output wire                     out_valid,
    input  wire                     out_ready,
    output wire signed [ACC_W-1:0]  out_c
);

    // ---------------- derived sizes (XGRP=4, ABTS=64, XW=128, PW=16) --------
    localparam XGRP  = COLS/LANES;            // x beats (groups) per transaction
    localparam ABTS  = (ROWS*COLS)/LANES;     // A beats per transaction
    localparam XW    = LANES*DATA_W;          // bits in one x group
    localparam GBW   = (XGRP <= 2) ? 1 : $clog2(XGRP);
    localparam ABW   = ($clog2(ABTS+1) < 2) ? 2 : $clog2(ABTS+1);
    localparam PW    = 2*DATA_W;              // product width
    // |row sum| <= COLS * 2^(2*DATA_W-2)  ->  2*DATA_W + ceil(log2(COLS)) + 1
    localparam ACCIW = 2*DATA_W + $clog2(COLS) + 1;

    reg  [ABW-1:0]           abeat;   // accepted A beats this transaction
    reg  [GBW:0]             xcnt;   // accepted x beats this transaction
    reg  [XGRP*XW-1:0]       xall;   // captured x groups
    reg  signed [ACCIW-1:0]  acc;    // running dot product of current row
    reg  signed [ACCIW-1:0]  outreg; // finished row awaiting handshake
    reg                      outv;

    wire [GBW-1:0] g = abeat[GBW-1:0];        // x group needed by this A beat

    // ---------------- helpers (combinational) -------------------------------
    // select x group <sel> out of the captured groups
    function [XW-1:0] xgroup;
        input [XGRP*XW-1:0] xa;
        input [GBW-1:0]     sel;
        integer k;
        begin
            xgroup = {XW{1'b0}};
            for (k = 0; k < XGRP; k = k + 1)
                if (sel == k) xgroup = xa[k*XW +: XW];
        end
    endfunction

    // sum of the LANES products plus the accumulator
    function signed [ACCIW-1:0] macc;
        input signed [ACCIW-1:0] a;
        input [LANES*PW-1:0]     pv;
        integer k;
        begin
            macc = a;
            for (k = 0; k < LANES; k = k + 1)
                macc = macc + $signed(pv[k*PW +: PW]);
        end
    endfunction

    // ---------------- x stream ---------------------------------------------
    wire xcan = (xcnt < XGRP);
    assign in_x_flat_ready = rst_n & xcan;
    wire x_acc  = in_x_flat_valid && rst_n && xcan;
    // this cycle's x beat is exactly the group the current A beat needs
    wire x_same = xcan && in_x_flat_valid && (xcnt[GBW-1:0] == g);
    // group already captured earlier
    wire x_have = (!xcan) || (xcnt[GBW-1:0] > g);
    wire x_avail = x_have || x_same;

    wire [XW-1:0] xg   = xgroup(xall, g);
    wire [XW-1:0] xsel = x_same ? in_x_flat : xg;

    // ---------------- LANES parallel 8x8 signed multipliers ---------------
    wire [LANES*PW-1:0] prodv;
    genvar pi;
    generate
        for (pi = 0; pi < LANES; pi = pi + 1) begin : PROD
            assign prodv[pi*PW +: PW] =
                $signed(in_a_flat[pi*DATA_W +: DATA_W]) *
                $signed(xsel[pi*DATA_W +: DATA_W]);
        end
    endgenerate

    // ---------------- accumulate (row partial sum) -------------------------
    wire signed [ACCIW-1:0] accadd = macc(acc, prodv);

    // ---------------- A stream ---------------------------------------------
    wire lastgrp   = (g == XGRP-1);          // last beat of the current row
    wire out_block = outv && !out_ready;      // finished row not yet drained
    wire a_run     = (abeat < ABTS);
    assign in_a_flat_ready = rst_n & a_run & x_avail & !(lastgrp & out_block);
    wire a_acc = in_a_flat_valid && in_a_flat_ready;

    // ---------------- output ----------------------------------------------
    assign out_valid = outv;
    assign out_c = outreg;                   // sign extends to ACC_W
    wire out_acc = outv && out_ready;
    wire txn_end = (abeat == ABTS) && out_acc;

    // ---------------- state -------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            abeat  <= {ABW{1'b0}};
            xcnt   <= {(GBW+1){1'b0}};
            acc    <= {ACCIW{1'b0}};
            outreg <= {ACCIW{1'b0}};
            outv   <= 1'b0;
        end else begin
            if (a_acc) begin
                abeat <= abeat + 1'b1;
                if (lastgrp) begin
                    outreg <= accadd;        // row finished
                    outv   <= 1'b1;
                    acc    <= {ACCIW{1'b0}};
                end else begin
                    acc <= accadd;
                end
            end

            // drain the finished row, unless a new one replaces it this cycle
            if (out_acc && !(a_acc && lastgrp))
                outv <= 1'b0;

            if (txn_end) begin               // transaction complete
                abeat <= {ABW{1'b0}};
                xcnt  <= {(GBW+1){1'b0}};
                acc   <= {ACCIW{1'b0}};
                outv  <= 1'b0;
            end
        end
    end

    // ---------------- x group capture ---------------------------------------
    // No reset needed: a group is only ever read after it has been written
    // within the same transaction.
    genvar gi;
    generate
        for (gi = 0; gi < XGRP; gi = gi + 1) begin : XGW
            always @(posedge clk) begin
                if (x_acc && (xcnt[GBW-1:0] == gi))
                    xall[gi*XW +: XW] <= in_x_flat;
            end
        end
    endgenerate

endmodule
