`default_nettype none
// C = A @ B, A: MxK (8x16) int8 row-major, B: KxN (16x8) int8 row-major,
// C: MxN (8x8) exact int32, row-major output, one word per beat.
//
// Architecture:
//  - B beats (128b = 2 B rows each) enter an 8-slot circular shift register.
//    Slot0 always presents the beat being consumed; one rotation per compute
//    step. Beat n is guaranteed to sit in slot0 when k-pair n fires:
//      * a beat may be accepted into slot0 directly when pair cur_p is
//        starving (cur_p == ringcnt),
//      * otherwise it lands in a 1-deep skid register (bpend) and is
//        inserted into slot0 at the fire edge where ringcnt == cur_p+1,
//      * or bypassed straight from the input wire at that fire edge.
//    Input gaps or backpressure on any stream can never corrupt or deadlock.
//    After row 0's 8 fires slot n holds beat n; each later row rotates the
//    ring exactly 8 times (identity), so pair p always reads beat p.
//  - A rows enter a row/hold buffer. The row register rotates 2 bytes per
//    compute step so the current k-pair is always at byte positions 0,1.
//  - 16 signed 8x8 multipliers process one k-pair for all 8 columns per
//    cycle; a row completes in 8 cycles into a parity-selected accumulator
//    buffer (double buffered so output overlaps the next row's compute).
//  - Accumulators are 20-bit signed (|C| <= 16*128*128 = 262144 fits);
//    out_c is the exact sign-extension to 32 bits.
//  - All per-transaction state is cleared when the last output word is
//    accepted, so back-to-back transactions work without reset.

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

    localparam SLOTW = LANES*DATA_W;     // 128 bits per beat
    localparam IAW   = 20;               // internal accumulator width

    // ---------------- B ring + skid ----------------
    reg [SLOTW-1:0] slot [0:7];
    reg [SLOTW-1:0] bpend;      // skid buffer holding beat `ringcnt`
    reg             bpend_valid;
    reg [3:0]       ringcnt;    // beats inserted into the ring (0..8)

    // ---------------- A row buffer (current + hold) ----------------
    reg [SLOTW-1:0] a_row;
    reg             row_valid;
    reg [SLOTW-1:0] a_hold;
    reg             hold_valid;

    // ---------------- counters / flags ----------------
    reg [3:0] cur_p;   // k-pair index (0..7) of row being computed
    reg [3:0] cur_r;   // row being computed (0..7; 8 = all rows done)
    reg [1:0] full;    // full[b]: accumulator buffer b holds a finished row
    reg [3:0] orow;    // row being output (0..7)
    reg [3:0] oword;   // output word index (0..7)

    wire tbuf = cur_r[0];   // accumulator buffer being computed
    wire obuf = orow[0];    // accumulator buffer being output

    wire last_row  = (cur_r == 4'd7);
    wire last_pair = (cur_p == 4'd7);

    // ---------------- compute step ----------------
    wire can_compute = row_valid && (cur_r < 4'd8) &&
                       (ringcnt > cur_p) && !full[tbuf];
    wire fire = can_compute;

    // ---------------- B input / ring insertion ----------------
    wire b_ready = !bpend_valid && (ringcnt < 4'd8);
    wire b_fire  = in_b_flat_valid && b_ready;

    wire need_next = (ringcnt == cur_p + 4'd1); // beat cur_p+1 not yet in ring
    wire ins_pend  = fire && need_next && bpend_valid;
    wire ins_byp   = fire && need_next && !bpend_valid && b_fire;
    wire ins_direct = b_fire && !fire && (cur_p == ringcnt);
    wire to_pend   = b_fire && !ins_byp && !ins_direct;

    // ---------------- A input ----------------
    wire a_ready = !hold_valid;
    wire a_fire  = in_a_flat_valid && a_ready;

    // ---------------- output ----------------
    wire out_fire = out_valid && out_ready;
    wire olast    = (oword == 4'd7);

    // ---------------- accumulators ----------------
    reg signed [IAW-1:0] acc [0:15]; // acc[{buf,j}]

    wire signed [DATA_W-1:0] a0 = a_row[7:0];
    wire signed [DATA_W-1:0] a1 = a_row[15:8];

    integer j;
    reg signed [IAW-1:0] osum;

    always @(posedge clk) begin
        if (fire) begin
            for (j = 0; j < N; j = j + 1) begin
                osum = $signed(a0) * $signed(slot[0][(j*DATA_W) +: DATA_W])
                     + $signed(a1) * $signed(slot[0][64+(j*DATA_W) +: DATA_W]);
                if (cur_p == 4'd0)
                    acc[{tbuf, j[2:0]}] <= osum;
                else
                    acc[{tbuf, j[2:0]}] <= acc[{tbuf, j[2:0]}] + osum;
            end
        end
    end

    // ---------------- sequential state ----------------
    integer i;
    always @(posedge clk) begin
        if (!rst_n) begin
            ringcnt    <= 4'd0;
            bpend_valid <= 1'b0;
            cur_p      <= 4'd0;
            cur_r      <= 4'd0;
            full       <= 2'b00;
            orow       <= 4'd0;
            oword      <= 4'd0;
            row_valid  <= 1'b0;
            hold_valid <= 1'b0;
        end else begin
            // B ring: rotate one slot per compute step; slot0 may be
            // overridden by a pending/bypassed/direct beat insertion.
            if (fire) begin
                for (i = 0; i < 7; i = i + 1)
                    slot[i] <= slot[i+1];
                slot[7] <= slot[0];
            end
            if (ins_pend) begin
                slot[0]     <= bpend;
                bpend_valid <= 1'b0;
                ringcnt     <= ringcnt + 4'd1;
            end else if (ins_byp || ins_direct) begin
                slot[0] <= in_b_flat;
                ringcnt <= ringcnt + 4'd1;
            end else if (to_pend) begin
                bpend       <= in_b_flat;
                bpend_valid <= 1'b1;
            end

            // A row path
            if (fire && last_pair) begin
                // row finished: next row comes from hold (or fresh beat);
                // on the very last row, drop the stale row so the next
                // transaction starts with an empty A path.
                if (hold_valid) begin
                    a_row      <= a_hold;
                    row_valid  <= a_fire || !last_row;
                    hold_valid <= a_fire;
                    if (a_fire)
                        a_hold <= in_a_flat;
                end else begin
                    row_valid <= a_fire;
                    if (a_fire)
                        a_row <= in_a_flat;
                end
            end else begin
                if (a_fire) begin
                    if (!row_valid) begin
                        a_row     <= in_a_flat;
                        row_valid <= 1'b1;
                    end else begin
                        a_hold     <= in_a_flat;
                        hold_valid <= 1'b1;
                    end
                end
                if (fire)
                    a_row <= {a_row[15:0], a_row[SLOTW-1:16]};
            end

            // compute counters / buffer-full flags
            if (fire) begin
                if (last_pair) begin
                    cur_p      <= 4'd0;
                    cur_r      <= cur_r + 4'd1;
                    full[tbuf] <= 1'b1;
                end else begin
                    cur_p <= cur_p + 4'd1;
                end
            end

            // output path
            if (out_fire) begin
                if (olast) begin
                    oword      <= 4'd0;
                    orow       <= orow + 4'd1;
                    full[obuf] <= 1'b0;
                    if (orow == 4'd7) begin
                        // transaction complete: clear per-transaction state
                        ringcnt <= 4'd0;
                        cur_p   <= 4'd0;
                        cur_r   <= 4'd0;
                        orow    <= 4'd0;
                        // full[0] cleared above; full[1] is already 0 here.
                        // bpend_valid is necessarily 0 at this point.
                    end
                end else begin
                    oword <= oword + 4'd1;
                end
            end
        end
    end

    // ---------------- output mux ----------------
    wire [3:0] oidx = {obuf, oword[2:0]};
    reg signed [IAW-1:0] osel;
    always @(*) begin
        osel = acc[oidx];
    end

    assign out_valid = full[obuf];
    assign out_c     = osel;   // exact sign-extension to ACC_W

    assign in_a_flat_ready = a_ready;
    assign in_b_flat_ready = b_ready;

endmodule
`default_nettype wire
