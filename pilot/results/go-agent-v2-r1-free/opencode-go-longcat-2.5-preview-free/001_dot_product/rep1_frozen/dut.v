// Dot product of LEN signed int8 elements, exact int32 result.
// Streams move LANES elements per beat; one beat is consumed per clock.
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

    localparam BEATS  = LEN / LANES;            // beats per transaction
    localparam PROD_W = 2 * DATA_W;             // exact product width
    localparam TREE_W = PROD_W + $clog2(LANES); // enough for LANES products
    localparam CNT_W  = $clog2(BEATS + 1);

    // ------------------------------------------------------------------
    // Control / state
    //
    // One register per port holds a beat that arrived before its partner,
    // so the two independent streams may be skewed by any amount.
    // ------------------------------------------------------------------
    reg  [ACC_W-1:0]        acc;
    reg  [CNT_W-1:0]        pair_cnt;
    reg                     done;
    reg                     have_a;
    reg                     have_b;
    reg  [LANES*DATA_W-1:0] hold_a;
    reg  [LANES*DATA_W-1:0] hold_b;

    wire a_fire = in_a_flat_valid & in_a_flat_ready;
    wire b_fire = in_b_flat_valid & in_b_flat_ready;

    // A held pair is consumed when both sides are present; a pair that
    // arrives on both ports in the same cycle is consumed immediately.
    wire both   = have_a & have_b;
    wire direct = a_fire & b_fire & ~have_a & ~have_b;
    wire acc_en = both | direct;

    assign in_a_flat_ready = ~done & (~have_a | have_b);
    assign in_b_flat_ready = ~done & (~have_b | have_a);

    wire [LANES*DATA_W-1:0] a_src = have_a ? hold_a : in_a_flat;
    wire [LANES*DATA_W-1:0] b_src = have_b ? hold_b : in_b_flat;

    // ------------------------------------------------------------------
    // Per-lane products: sign-extend each int8 element to PROD_W bits,
    // then signed-multiply (exact 16 bit result).
    // ------------------------------------------------------------------
    wire [LANES*PROD_W-1:0] a_ext;
    wire [LANES*PROD_W-1:0] b_ext;
    wire [LANES*PROD_W-1:0] prod_flat;

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_ext
            assign a_ext[gi*PROD_W +: PROD_W] =
                { {(PROD_W-DATA_W){a_src[gi*DATA_W + DATA_W - 1]}},
                  a_src[gi*DATA_W +: DATA_W] };
            assign b_ext[gi*PROD_W +: PROD_W] =
                { {(PROD_W-DATA_W){b_src[gi*DATA_W + DATA_W - 1]}},
                  b_src[gi*DATA_W +: DATA_W] };
        end
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_prod
            assign prod_flat[gi*PROD_W +: PROD_W] =
                $signed(a_ext[gi*PROD_W +: PROD_W]) * $signed(b_ext[gi*PROD_W +: PROD_W]);
        end
    endgenerate

    // ------------------------------------------------------------------
    // Reduction chain for the LANES products of one beat
    // ------------------------------------------------------------------
    wire signed [LANES*TREE_W-1:0] chain_flat;

    assign chain_flat[0 +: TREE_W] = prod_flat[0 +: PROD_W];
    generate
        for (gi = 1; gi < LANES; gi = gi + 1) begin : g_chain
            assign chain_flat[gi*TREE_W +: TREE_W] =
                chain_flat[(gi-1)*TREE_W +: TREE_W] + prod_flat[gi*PROD_W +: PROD_W];
        end
    endgenerate

    wire signed [ACC_W-1:0] partial =
        { {(ACC_W-TREE_W){chain_flat[LANES*TREE_W - 1]}},
          chain_flat[LANES*TREE_W - TREE_W +: TREE_W] };

    // ------------------------------------------------------------------
    // Output
    // ------------------------------------------------------------------
    assign out_valid = done;
    assign out_c     = acc;

    wire out_fire = out_valid & out_ready;

    always @(posedge clk) begin
        if (!rst_n) begin
            acc      <= {ACC_W{1'b0}};
            pair_cnt <= {CNT_W{1'b0}};
            done     <= 1'b0;
            have_a   <= 1'b0;
            have_b   <= 1'b0;
            hold_a   <= {(LANES*DATA_W){1'b0}};
            hold_b   <= {(LANES*DATA_W){1'b0}};
        end else if (out_fire) begin
            // Transaction finished: clear per-transaction state.  Beats
            // already captured in the hold registers belong to the next
            // transaction and are kept.
            acc      <= {ACC_W{1'b0}};
            pair_cnt <= {CNT_W{1'b0}};
            done     <= 1'b0;
        end else if (!done) begin
            if (a_fire) hold_a <= in_a_flat;
            if (b_fire) hold_b <= in_b_flat;

            if (a_fire) have_a <= ~direct;
            else        have_a <= both ? 1'b0 : have_a;

            if (b_fire) have_b <= ~direct;
            else        have_b <= both ? 1'b0 : have_b;

            if (acc_en) begin
                acc <= acc + partial;
                if (pair_cnt == BEATS-1) begin
                    done     <= 1'b1;
                    pair_cnt <= {CNT_W{1'b0}};
                end else begin
                    pair_cnt <= pair_cnt + 1'b1;
                end
            end
        end
    end

endmodule
