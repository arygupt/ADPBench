// 001_dot_product : c = sum_{i=0}^{LEN-1} a[i]*b[i]   (signed int8 -> exact int32)
//
// Two independent valid/ready input streams, LANES elements per beat packed
// lane-major / LSB first.  Each stream owns its own beat buffer and its own
// accept counter, so a port may run ahead or stall without disturbing the
// other; a beat pair is reduced P lanes per cycle into an exact signed
// accumulator.
//
// With P == LANES a whole beat pair is reduced on the cycle it is accepted
// (folded straight out of the input wires), so a synchronised pair of streams
// costs one cycle per beat and the buffers only exist to absorb skew.
//
// The last reduction result is exposed combinationally so the output word can
// be taken on the very cycle it is produced.  When the final beat pair of a
// transaction has been reduced every piece of per-transaction state is
// cleared, which makes reset-less back-to-back transactions work.

module dut #(
    parameter LEN    = 256,
    parameter LANES  = 32,
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

    // ---------------------------------------------------------------- sizing
    localparam integer P     = 32;              // products evaluated per cycle
    localparam integer C     = LANES / P;       // cycles needed for one beat
    localparam integer BEATS = LEN / LANES;     // beats per transaction
    localparam integer PW    = 2*DATA_W + $clog2(P) + 2;   // partial sum width

    // ------------------------------------------------------------ beat state
    reg [LANES*DATA_W-1:0] aq;      // lane j lives at [j*DATA_W +: DATA_W]
    reg [LANES*DATA_W-1:0] bq;
    reg                    va;      // aq holds a beat not yet fully reduced
    reg                    vb;
    reg [3:0]              na;      // beats accepted on port a (this txn)
    reg [3:0]              nb;
    reg [3:0]              sc;      // lane-group index inside the current beat

    reg signed [ACC_W-1:0] acc;     // exact running accumulator
    reg signed [ACC_W-1:0] out_r;   // result register (when not bypassed)
    reg                    ov;      // result register is valid

    // ------------------------------------------------------- handshake logic
    wire proc  = va && vb;                  // a whole beat pair is buffered
    wire frees = proc && (sc == C-1);       // buffers drain at this edge

    assign in_a_flat_ready = (na < BEATS) && !ov && (!va || frees);
    assign in_b_flat_ready = (nb < BEATS) && !ov && (!vb || frees);

    wire acc_a = in_a_flat_ready && in_a_flat_valid;
    wire acc_b = in_b_flat_ready && in_b_flat_valid;

    // beat pair landing into empty buffers: fold it directly out of the wires
    wire fast = acc_a && acc_b && !va && !vb;
    wire work = proc || fast;

    wire [P*DATA_W-1:0] asrc = fast ? in_a_flat[P*DATA_W-1:0]
                                    : aq[sc*P*DATA_W +: P*DATA_W];
    wire [P*DATA_W-1:0] bsrc = fast ? in_b_flat[P*DATA_W-1:0]
                                    : bq[sc*P*DATA_W +: P*DATA_W];

    // ----------------------------------------------------------- reduction
    integer i;
    reg signed [PW-1:0] psum;
    always @* begin
        psum = {PW{1'b0}};
        for (i = 0; i < P; i = i + 1)
            psum = psum + ($signed(asrc[i*DATA_W +: DATA_W]) *
                           $signed(bsrc[i*DATA_W +: DATA_W]));
    end

    wire signed [ACC_W-1:0] total = acc + psum;

    // a folded beat is already spent, a buffered one is only spent once its
    // whole lane range has been walked
    wire store_a = acc_a && !(fast && (C == 1));
    wire store_b = acc_b && !(fast && (C == 1));

    // index of the beat pair being drained, and the end-of-transaction marker
    wire [3:0] a_idx = va ? na : (na + 4'd1);
    wire [3:0] b_idx = vb ? nb : (nb + 4'd1);
    wire       done  = work && (sc == C-1) &&
                       (a_idx == BEATS) && (b_idx == BEATS);

    // ------------------------------------------------------------ sequencing
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            aq    <= {LANES*DATA_W{1'b0}};
            bq    <= {LANES*DATA_W{1'b0}};
            va    <= 1'b0;
            vb    <= 1'b0;
            na    <= 4'd0;
            nb    <= 4'd0;
            sc    <= 4'd0;
            acc   <= {ACC_W{1'b0}};
            out_r <= {ACC_W{1'b0}};
            ov    <= 1'b0;
        end else begin
            // accumulate the current lane group
            if (work) begin
                if (done) begin
                    if (!out_ready) begin      // otherwise taken combinationally
                        out_r <= total;
                        ov    <= 1'b1;
                    end
                    acc <= {ACC_W{1'b0}};
                end else begin
                    acc <= total;
                end
                sc <= (fast && (C > 1)) ? 4'd1
                                        : ((sc == C-1) ? 4'd0 : (sc + 4'd1));
            end else begin
                sc <= 4'd0;
            end

            // beat buffers / accept counters
            if (done) begin
                va <= 1'b0;
                vb <= 1'b0;
                na <= 4'd0;
                nb <= 4'd0;
            end else begin
                if (acc_a) na <= na + 4'd1;
                if (acc_b) nb <= nb + 4'd1;

                if (store_a) begin
                    aq <= in_a_flat;
                    va <= 1'b1;
                end else if (frees) begin
                    va <= 1'b0;
                end

                if (store_b) begin
                    bq <= in_b_flat;
                    vb <= 1'b1;
                end else if (frees) begin
                    vb <= 1'b0;
                end
            end

            if (ov && out_ready)
                ov <= 1'b0;
        end
    end

    assign out_valid = ov || done;
    assign out_c     = done ? total : out_r;

endmodule
