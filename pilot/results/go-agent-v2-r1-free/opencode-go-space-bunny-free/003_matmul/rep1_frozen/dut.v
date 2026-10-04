// -----------------------------------------------------------------------------
// 003_matmul : C = A (MxK) @ B (KxN), int8 in / int32 out, bit exact.
//
// Datapath
// --------
// One MAC step per cycle, N = 8 multipliers sharing the operand A[i][k] and
// taking the N bytes of B row k:
//     acc[i][j] <= acc[i][j] + A[i][k]*B[k][j]
// A row of C needs K = 16 steps, the eight rows need 128 cycles.  The eight
// output words of a row leave at one word per cycle (8 cycles) while the next
// row is being accumulated, so emission never waits for compute and a whole
// transaction costs 8 input beats + 128 compute cycles + 8 drain cycles.
//
// Four accumulator banks are addressed by row[1:0]; a step is suppressed while
// the row being emitted shares its bank.  That never happens at full speed
// (compute is exactly one row ahead of emission) but keeps the design correct
// for any output backpressure pattern.
//
// Storage: A rows are stored as received (one K*DATA_W register per beat, since
// K == LANES); B rows are stored as received (N*DATA_W registers, LANES/N = 2
// rows per beat).  Writes are constant data with a decoded enable and each
// array has a single asynchronous read port.
//
// A new transaction is recognised on the first accepted beat after both input
// counters reached their end; input ready is withheld until the current
// transaction has been fully emitted, so no state is ever clobbered.
// -----------------------------------------------------------------------------
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

    localparam integer AB  = (M*K)/LANES;  // A beats per transaction
    localparam integer BB  = (K*N)/LANES;  // B beats per transaction
    localparam integer AW  = 20;           // internal accumulator width
    localparam integer NB  = 4;            // accumulator banks
    localparam integer CW  = 4;

    // ---------------------------------------------------------------- state --
    reg  [CW-1:0]        a_cnt;    // accepted A beats
    reg  [CW-1:0]        b_cnt;    // accepted B beats
    reg  [CW-1:0]        rdone;    // rows of C fully accumulated
    reg  [CW-1:0]        cur_row;  // row being accumulated
    reg  [CW-1:0]        orow;     // output row pointer, 0..M
    reg  [2:0]           ocol;     // output column pointer
    reg  [3:0]           ck;       // k index inside the row

    reg  [K*DATA_W-1:0]  arow [0:M-1];   // arow[i] = A[i][0..K-1]
    reg  [N*DATA_W-1:0]  breg [0:K-1];   // breg[k] = B[k][0..N-1]
    reg  signed [AW-1:0] acc [0:NB-1][0:N-1];

    // ------------------------------------------------------------ handshake --
    wire indone  = (a_cnt == AB) && (b_cnt == BB);
    wire outdone = (orow == M);
    wire idle    = indone && outdone;

    // A may only complete (a_cnt == AB) once B has completed and vice versa,
    // so both counters reach their end together and a following beat, if any,
    // unambiguously belongs to the next transaction.
    assign in_a_flat_ready = idle || ((a_cnt < AB) && ((a_cnt < (AB-1)) || (b_cnt == BB)));
    assign in_b_flat_ready = idle || ((b_cnt < BB) && ((b_cnt < (BB-1)) || (a_cnt >= (AB-1))));

    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire b_fire = in_b_flat_valid && in_b_flat_ready;
    wire newtxn = ((a_cnt == AB) && a_fire) || ((b_cnt == BB) && b_fire);

    wire [CW-1:0] a_wi = (a_cnt == AB) ? {CW{1'b0}} : a_cnt;   // A row written
    wire [CW-1:0] b_wi = (b_cnt == BB) ? {CW{1'b0}} : b_cnt;   // B beat written

    // -------------------------------------------------------- read datapath --
    reg [K*DATA_W-1:0] arow_sel;
    reg [N*DATA_W-1:0] brow_sel;
    integer ii;
    always @* begin
        arow_sel = {K*DATA_W{1'b0}};
        brow_sel = {N*DATA_W{1'b0}};
        for (ii = 0; ii < M; ii = ii + 1)
            if (cur_row == ii) arow_sel = arow[ii];
        for (ii = 0; ii < K; ii = ii + 1)
            if (ck == ii) brow_sel = breg[ii];
    end

    wire signed [DATA_W-1:0] a_val = $signed(arow_sel[ck*DATA_W +: DATA_W]);

    // ------------------------------------------------------- MAC + accumulate --
    wire signed [AW-1:0] pext [0:N-1];
    genvar gj;
    generate
        for (gj = 0; gj < N; gj = gj + 1) begin : gmac
            wire signed [2*DATA_W-1:0] q = a_val * $signed(brow_sel[gj*DATA_W +: DATA_W]);
            assign pext[gj] = {{(AW-2*DATA_W){q[2*DATA_W-1]}}, q};
        end
    endgenerate

    // ------------------------------------------------------- MAC sequencer --
    // A row i needs A beat i; B row k arrives with beat k/2.
    wire data_ok = ({1'b0, cur_row} < {1'b0, a_cnt})
                   && ({1'b0, ck[3:1]} < {1'b0, b_cnt});
    wire conflict = (orow < rdone) && (orow[1:0] == cur_row[1:0]);
    wire comp_en  = (cur_row < M) && data_ok && !conflict;
    wire first    = (ck == 4'd0);          // first k of the row: load, not add

    // ---------------------------------------------------------------- output --
    wire signed [AW-1:0] ow = acc[orow[1:0]][ocol];
    assign out_valid = (orow < rdone);
    assign out_c     = {{(ACC_W-AW){ow[AW-1]}}, ow};
    wire out_fire = out_valid && out_ready;

    integer jb, jc;
    always @(posedge clk) begin
        if (!rst_n) begin
            a_cnt   <= {CW{1'b0}};
            b_cnt   <= {CW{1'b0}};
            rdone   <= {CW{1'b0}};
            cur_row <= {CW{1'b0}};
            orow    <= {CW{1'b0}};
            ocol    <= 3'd0;
            ck      <= 4'd0;
            for (jb = 0; jb < NB; jb = jb + 1)
                for (jc = 0; jc < N; jc = jc + 1)
                    acc[jb][jc] <= {AW{1'b0}};
        end else begin
            // input capture: constant data, decoded write enable
            if (a_fire) arow[a_wi] <= in_a_flat;
            if (b_fire) begin
                breg[{b_wi, 1'b0}] <= in_b_flat[N*DATA_W-1:0];
                breg[{b_wi, 1'b1}] <= in_b_flat[2*N*DATA_W-1:N*DATA_W];
            end

            if (newtxn) begin
                a_cnt   <= a_fire ? {{(CW-1){1'b0}},1'b1} : {CW{1'b0}};
                b_cnt   <= b_fire ? {{(CW-1){1'b0}},1'b1} : {CW{1'b0}};
                rdone   <= {CW{1'b0}};
                cur_row <= {CW{1'b0}};
                orow    <= {CW{1'b0}};
                ocol    <= 3'd0;
                ck      <= 4'd0;
                for (jb = 0; jb < NB; jb = jb + 1)
                    for (jc = 0; jc < N; jc = jc + 1)
                        acc[jb][jc] <= {AW{1'b0}};
            end else begin
                if (a_fire) a_cnt <= a_cnt + {{(CW-1){1'b0}},1'b1};
                if (b_fire) b_cnt <= b_cnt + {{(CW-1){1'b0}},1'b1};

                if (out_fire) begin
                    if (ocol == (N-1)) begin
                        ocol <= 3'd0;
                        orow <= orow + {{(CW-1){1'b0}},1'b1};
                    end else begin
                        ocol <= ocol + 3'd1;
                    end
                end

                if (comp_en) begin
                    if (ck == (K-1)) begin
                        ck      <= 4'd0;
                        cur_row <= cur_row + {{(CW-1){1'b0}},1'b1};
                        rdone   <= rdone + {{(CW-1){1'b0}},1'b1};
                    end else begin
                        ck <= ck + 4'd1;
                    end
                    for (jc = 0; jc < N; jc = jc + 1) begin
                        if (first)
                            acc[cur_row[1:0]][jc] <= pext[jc];
                        else
                            acc[cur_row[1:0]][jc] <= acc[cur_row[1:0]][jc] + pext[jc];
                    end
                end
            end
        end
    end

endmodule
