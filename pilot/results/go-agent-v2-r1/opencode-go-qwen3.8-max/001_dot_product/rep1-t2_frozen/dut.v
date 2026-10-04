// 001_dot_product: c = sum_i a[i]*b[i], int8 two's-complement inputs,
// exact int32 output. LEN=256, LANES=32/beat, BEATS=8/transaction.
//
// Architecture (area-optimized, adder-based):
//  - Per lane: sign-magnitude int8 multiply. ma = (a ^ sa) + sa = |a|;
//    an unsigned slice-matched ripple array (7 narrow 9-bit $add stages)
//    forms p = ma*mb; the lane sign s = sa^sb folds into the global tree as
//    q = p ^ {16{s}} (16-bit one's complement) while the +1 of each
//    two's-complement negation is collected by the sign popcount spc.
//    Exact identity: q = s ? (2^16 - p) : p, so
//    beat_sum = (sum_i q_i + spc) - spc*2^16 = sum_i (s_i ? -p_i : p_i).
//  - Global unsigned $add tree sums the 32 q values plus spc (<= 2^21).
//  - Narrow 24-bit signed running accumulator; |result| <= 256*128*128 = 2^22.
//  - Independent single-beat skid buffers per input stream; output is driven
//    combinationally on the last beat pair (registered fallback when
//    out_ready is low), so a lock-step transaction takes 8 cycles and
//    back-to-back transactions / backpressure are supported.
module dut #(
    parameter LEN     = 256,
    parameter LANES   = 32,
    parameter DATA_W  = 8,
    parameter ACC_W   = 32
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
    localparam integer BEATS  = LEN / LANES;               // 8
    localparam integer CNT_W  = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam integer PW     = 2 * DATA_W;                // 16
    localparam integer ACCI_W = PW + $clog2(LEN);          // 24

    // ---------------- stream skid buffers / beat pairing ----------------
    reg [LANES*DATA_W-1:0] a_buf, b_buf;
    reg                    a_have, b_have;
    reg  [CNT_W-1:0]         beat_cnt;
    reg  signed [ACCI_W-1:0] acc;
    reg  signed [ACC_W-1:0]  out_q;
    reg                      out_v;

    wire a_new = in_a_flat_valid & ~a_have;
    wire b_new = in_b_flat_valid & ~b_have;
    wire a_av  = a_have | in_a_flat_valid;
    wire b_av  = b_have | in_b_flat_valid;
    wire pair  = a_av & b_av;
    wire lastb = (beat_cnt == BEATS - 1);
    wire lastpair = pair & lastb;

    assign in_a_flat_ready = ~a_have;
    assign in_b_flat_ready = ~b_have;

    wire [LANES*DATA_W-1:0] a_sel = a_have ? a_buf : in_a_flat;
    wire [LANES*DATA_W-1:0] b_sel = b_have ? b_buf : in_b_flat;

    // ---------------- per-lane magnitude products and sign folding -------
    wire [PW-1:0] qlane [0:LANES-1];   // s ? 2^16-p : p   (p = |a|*|b|)
    wire [LANES-1:0] svec;             // per-lane product sign bits
    genvar gi, gk;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_ln
            wire [7:0] araw = a_sel[gi*8 +: 8];
            wire [7:0] braw = b_sel[gi*8 +: 8];
            wire sa = araw[7];
            wire sb = braw[7];
            assign svec[gi] = sa ^ sb;
            wire [7:0] ma = (araw ^ {8{sa}}) + {7'b0, sa};   // |a|
            wire [7:0] mb = (braw ^ {8{sb}}) + {7'b0, sb};   // |b|
            // unsigned slice-matched ripple array; t_k holds cols 0..k+7
            // (t_k <= 255*(2^(k+1)-1), so each stage's shifted addend fits
            //  8 bits and every stage adder is 9 bits wide)
            wire [7:0]  r0 = ma & {8{mb[0]}};
            wire [14:0] t0 = {7'b0, r0};
            wire [7:0]  r1 = ma & {8{mb[1]}};
            wire [8:0]  x1 = {1'b0, t0[7:1]}  + {1'b0, r1};
            wire [14:0] t1 = {5'b0, x1, t0[0]};
            wire [7:0]  r2 = ma & {8{mb[2]}};
            wire [8:0]  x2 = {1'b0, t1[9:2]}  + {1'b0, r2};
            wire [14:0] t2 = {4'b0, x2, t1[1:0]};
            wire [7:0]  r3 = ma & {8{mb[3]}};
            wire [8:0]  x3 = {1'b0, t2[10:3]} + {1'b0, r3};
            wire [14:0] t3 = {3'b0, x3, t2[2:0]};
            wire [7:0]  r4 = ma & {8{mb[4]}};
            wire [8:0]  x4 = {1'b0, t3[11:4]} + {1'b0, r4};
            wire [14:0] t4 = {2'b0, x4, t3[3:0]};
            wire [7:0]  r5 = ma & {8{mb[5]}};
            wire [8:0]  x5 = {1'b0, t4[12:5]} + {1'b0, r5};
            wire [14:0] t5 = {1'b0, x5, t4[4:0]};
            wire [7:0]  r6 = ma & {8{mb[6]}};
            wire [8:0]  x6 = {1'b0, t5[13:6]} + {1'b0, r6};
            wire [14:0] t6 = {x6, t5[5:0]};
            wire [7:0]  r7 = ma & {8{mb[7]}};
            wire [8:0]  x7 = {1'b0, t6[14:7]} + {1'b0, r7};
            wire [15:0] p  = {x7, t6[6:0]};                  // ma*mb <= 16384
            assign qlane[gi] = p ^ {16{svec[gi]}};           // one's compl.
        end
    endgenerate

    // ---------------- sign popcount (spc <= 32) ----------------
    wire [3:0] pc0 = {3'd0, svec[0]}  + {3'd0, svec[1]}  + {3'd0, svec[2]}
                   + {3'd0, svec[3]}  + {3'd0, svec[4]}  + {3'd0, svec[5]}
                   + {3'd0, svec[6]}  + {3'd0, svec[7]};
    wire [3:0] pc1 = {3'd0, svec[8]}  + {3'd0, svec[9]}  + {3'd0, svec[10]}
                   + {3'd0, svec[11]} + {3'd0, svec[12]} + {3'd0, svec[13]}
                   + {3'd0, svec[14]} + {3'd0, svec[15]};
    wire [3:0] pc2 = {3'd0, svec[16]} + {3'd0, svec[17]} + {3'd0, svec[18]}
                   + {3'd0, svec[19]} + {3'd0, svec[20]} + {3'd0, svec[21]}
                   + {3'd0, svec[22]} + {3'd0, svec[23]};
    wire [3:0] pc3 = {3'd0, svec[24]} + {3'd0, svec[25]} + {3'd0, svec[26]}
                   + {3'd0, svec[27]} + {3'd0, svec[28]} + {3'd0, svec[29]}
                   + {3'd0, svec[30]} + {3'd0, svec[31]};
    wire [5:0] spc = {2'd0, pc0} + {2'd0, pc1} + {2'd0, pc2} + {2'd0, pc3};

    // ---------------- global unsigned reduction tree ----------------
    wire [16:0] u1_0  = {1'b0, qlane[0]}  + {1'b0, qlane[1]};
    wire [16:0] u1_1  = {1'b0, qlane[2]}  + {1'b0, qlane[3]};
    wire [16:0] u1_2  = {1'b0, qlane[4]}  + {1'b0, qlane[5]};
    wire [16:0] u1_3  = {1'b0, qlane[6]}  + {1'b0, qlane[7]};
    wire [16:0] u1_4  = {1'b0, qlane[8]}  + {1'b0, qlane[9]};
    wire [16:0] u1_5  = {1'b0, qlane[10]} + {1'b0, qlane[11]};
    wire [16:0] u1_6  = {1'b0, qlane[12]} + {1'b0, qlane[13]};
    wire [16:0] u1_7  = {1'b0, qlane[14]} + {1'b0, qlane[15]};
    wire [16:0] u1_8  = {1'b0, qlane[16]} + {1'b0, qlane[17]};
    wire [16:0] u1_9  = {1'b0, qlane[18]} + {1'b0, qlane[19]};
    wire [16:0] u1_10 = {1'b0, qlane[20]} + {1'b0, qlane[21]};
    wire [16:0] u1_11 = {1'b0, qlane[22]} + {1'b0, qlane[23]};
    wire [16:0] u1_12 = {1'b0, qlane[24]} + {1'b0, qlane[25]};
    wire [16:0] u1_13 = {1'b0, qlane[26]} + {1'b0, qlane[27]};
    wire [16:0] u1_14 = {1'b0, qlane[28]} + {1'b0, qlane[29]};
    wire [16:0] u1_15 = {1'b0, qlane[30]} + {1'b0, qlane[31]};

    wire [17:0] u2_0 = {1'b0, u1_0}  + {1'b0, u1_1};
    wire [17:0] u2_1 = {1'b0, u1_2}  + {1'b0, u1_3};
    wire [17:0] u2_2 = {1'b0, u1_4}  + {1'b0, u1_5};
    wire [17:0] u2_3 = {1'b0, u1_6}  + {1'b0, u1_7};
    wire [17:0] u2_4 = {1'b0, u1_8}  + {1'b0, u1_9};
    wire [17:0] u2_5 = {1'b0, u1_10} + {1'b0, u1_11};
    wire [17:0] u2_6 = {1'b0, u1_12} + {1'b0, u1_13};
    wire [17:0] u2_7 = {1'b0, u1_14} + {1'b0, u1_15};

    wire [18:0] u3_0 = {1'b0, u2_0} + {1'b0, u2_1};
    wire [18:0] u3_1 = {1'b0, u2_2} + {1'b0, u2_3};
    wire [18:0] u3_2 = {1'b0, u2_4} + {1'b0, u2_5};
    wire [18:0] u3_3 = {1'b0, u2_6} + {1'b0, u2_7};

    wire [19:0] u4_0 = {1'b0, u3_0} + {1'b0, u3_1};
    wire [19:0] u4_1 = {1'b0, u3_2} + {1'b0, u3_3};

    wire [20:0] u5 = {1'b0, u4_0} + {1'b0, u4_1};

    wire [21:0] beat_total = {1'b0, u5} + {14'b0, spc};

    // remove the per-negative-lane 2^16 offset of the one's complements:
    // beat_sum = (sum q_i + spc) - spc*2^16 = exact signed beat dot product
    wire [21:0] bt = beat_total - {spc[5:0], 16'b0};
    wire signed [ACCI_W-1:0] beat_sum = $signed({{2{bt[21]}}, bt});

    wire signed [ACCI_W-1:0] live = acc + beat_sum;

    wire signed [ACC_W-1:0] res_live =
        {{(ACC_W-ACCI_W){live[ACCI_W-1]}}, live};

    assign out_valid = out_v | lastpair;
    assign out_c     = out_v ? out_q : res_live;

    // ---------------- sequential control ----------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_have   <= 1'b0;
            b_have   <= 1'b0;
            a_buf    <= {(LANES*DATA_W){1'b0}};
            b_buf    <= {(LANES*DATA_W){1'b0}};
            beat_cnt <= {CNT_W{1'b0}};
            acc      <= {ACCI_W{1'b0}};
            out_v    <= 1'b0;
            out_q    <= {ACC_W{1'b0}};
        end else begin
            if (pair) begin
                a_have <= 1'b0;
                b_have <= 1'b0;
                if (lastb) begin
                    beat_cnt <= {CNT_W{1'b0}};
                    acc      <= {ACCI_W{1'b0}};
                    if (out_v | ~out_ready) begin
                        out_q <= res_live;
                        out_v <= 1'b1;
                    end
                end else begin
                    beat_cnt <= beat_cnt + 1'b1;
                    acc      <= live;
                end
            end else begin
                if (a_new) begin
                    a_buf  <= in_a_flat;
                    a_have <= 1'b1;
                end
                if (b_new) begin
                    b_buf  <= in_b_flat;
                    b_have <= 1'b1;
                end
            end
            if (out_v && out_ready && !lastpair)
                out_v <= 1'b0;
        end
    end
endmodule
