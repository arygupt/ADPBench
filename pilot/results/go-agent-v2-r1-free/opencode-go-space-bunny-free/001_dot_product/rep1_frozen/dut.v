// Dot product: out_c = sum_{i=0}^{LEN-1} a[i]*b[i], signed int8 in, exact int32 out.
//
// Both input streams are accepted at full rate (a beat is refused only when its
// private beat buffer is completely full), so input acceptance never depends on
// the other stream's progress nor on output back-pressure.  Beats are paired by
// index; a beat is consumed as soon as its partner is available (directly from
// the input ports when both arrive together, otherwise out of the buffers).
// LANES parallel signed multipliers + adder tree produce one beat-sum per cycle,
// which is accumulated and pushed into a 2 deep output queue at the end of every
// transaction, so a pending output never stalls the datapath.

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

    localparam integer BEATS    = LEN / LANES;              // beats per transaction
    localparam integer AW       = LANES * DATA_W;
    localparam integer PW       = (BEATS <= 1) ? 1 : $clog2(BEATS);
    localparam integer CW       = (BEATS <= 1) ? 1 : $clog2(BEATS + 1);
    localparam integer PROD_W   = 2 * DATA_W;
    localparam integer ACC_BITS = PROD_W + $clog2(LEN) + 1;

    // ---------------- beat buffers (one per input stream) ----------------
    reg [AW-1:0] memA [0:BEATS-1];
    reg [AW-1:0] memB [0:BEATS-1];

    reg [PW-1:0] wa, ra, wb, rb;
    reg [CW-1:0] ca, cb;                       // occupancy, 0..BEATS

    assign in_a_flat_ready = (ca != BEATS[CW-1:0]);
    assign in_b_flat_ready = (cb != BEATS[CW-1:0]);

    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire b_fire = in_b_flat_valid && in_b_flat_ready;

    wire useA_mem = (ca != {CW{1'b0}});
    wire useB_mem = (cb != {CW{1'b0}});

    wire a_src = useA_mem || a_fire;
    wire b_src = useB_mem || b_fire;
    wire do_c  = a_src && b_src;               // a matched pair is available

    // A fired beat is written to the buffer unless it is the beat consumed right
    // now (that only happens when the buffer was empty).
    wire a_take  = do_c && useA_mem;
    wire b_take  = do_c && useB_mem;
    wire a_store = a_fire && (useA_mem || !do_c);
    wire b_store = b_fire && (useB_mem || !do_c);

    wire [AW-1:0] a_word = useA_mem ? memA[ra] : in_a_flat;
    wire [AW-1:0] b_word = useB_mem ? memB[rb] : in_b_flat;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wa <= {PW{1'b0}};
            wb <= {PW{1'b0}};
            ca <= {CW{1'b0}};
            cb <= {CW{1'b0}};
        end else begin
            if (a_store) begin
                memA[wa] <= in_a_flat;
                wa <= wa + 1'b1;
            end
            if (a_take) ra <= ra + 1'b1;
            case ({a_store, a_take})
                2'b10:   ca <= ca + 1'b1;
                2'b01:   ca <= ca - 1'b1;
                default: ;
            endcase

            if (b_store) begin
                memB[wb] <= in_b_flat;
                wb <= wb + 1'b1;
            end
            if (b_take) rb <= rb + 1'b1;
            case ({b_store, b_take})
                2'b10:   cb <= cb + 1'b1;
                2'b01:   cb <= cb - 1'b1;
                default: ;
            endcase
        end
    end

    // ---------------- lane multipliers ----------------
    wire signed [PROD_W-1:0] prod [0:LANES-1];

    genvar j;
    generate
        for (j = 0; j < LANES; j = j + 1) begin : g_mul
            wire signed [DATA_W-1:0] a_j;
            wire signed [DATA_W-1:0] b_j;
            assign a_j = a_word[j*DATA_W +: DATA_W];
            assign b_j = b_word[j*DATA_W +: DATA_W];
            assign prod[j] = a_j * b_j;
        end
    endgenerate

    // ---------------- adder tree ----------------
    wire signed [PROD_W:0]   s1 [0:LANES/2-1];
    wire signed [PROD_W+1:0] s2 [0:LANES/4-1];
    wire signed [PROD_W+2:0] s3 [0:LANES/8-1];
    wire signed [PROD_W+3:0] s4 [0:LANES/16-1];
    wire signed [PROD_W+4:0] s5 [0:LANES/32-1];

    generate
        for (j = 0; j < LANES/2; j = j + 1) begin : g_l1
            assign s1[j] = prod[2*j] + prod[2*j+1];
        end
        for (j = 0; j < LANES/4; j = j + 1) begin : g_l2
            assign s2[j] = s1[2*j] + s1[2*j+1];
        end
        for (j = 0; j < LANES/8; j = j + 1) begin : g_l3
            assign s3[j] = s2[2*j] + s2[2*j+1];
        end
        for (j = 0; j < LANES/16; j = j + 1) begin : g_l4
            assign s4[j] = s3[2*j] + s3[2*j+1];
        end
        for (j = 0; j < LANES/32; j = j + 1) begin : g_l5
            assign s5[j] = s4[2*j] + s4[2*j+1];
        end
    endgenerate

    wire signed [ACC_BITS-1:0] beat_sum;
    assign beat_sum = {{(ACC_BITS-(PROD_W+5)){s5[0][PROD_W+4]}}, s5[0]};

    // ---------------- accumulation / transaction framing ----------------
    reg [PW-1:0]       bidx;
    reg [ACC_BITS-1:0] acc;

    // 2 deep output queue: oq0 holds the head (oldest pending word)
    reg [ACC_BITS-1:0] oq0, oq1;
    reg [1:0]          ocnt;

    wire signed [ACC_BITS-1:0] acc_sum;
    assign acc_sum = $signed(acc) + $signed(beat_sum);

    wire do_push = do_c && (bidx == (BEATS-1));
    wire do_pop  = (ocnt != 2'd0) && out_ready;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            bidx <= {PW{1'b0}};
            acc  <= {ACC_BITS{1'b0}};
            oq0  <= {ACC_BITS{1'b0}};
            oq1  <= {ACC_BITS{1'b0}};
            ocnt <= 2'd0;
        end else begin
            if (do_c) begin
                bidx <= bidx + 1'b1;
                // the accumulator covers exactly one transaction
                acc  <= do_push ? {ACC_BITS{1'b0}} : acc_sum;
            end

            case ({do_push, do_pop})
                2'b10: begin                       // push only
                    if (ocnt == 2'd0) begin
                        oq0  <= acc_sum;
                        ocnt <= 2'd1;
                    end else begin
                        oq1  <= acc_sum;
                        ocnt <= 2'd2;
                    end
                end
                2'b01: begin                       // pop only
                    if (ocnt == 2'd2) begin
                        oq0  <= oq1;
                        ocnt <= 2'd1;
                    end else begin
                        ocnt <= 2'd0;
                    end
                end
                2'b11: begin                       // pop and push together
                    if (ocnt == 2'd2) begin
                        oq0  <= oq1;
                        oq1  <= acc_sum;
                        ocnt <= 2'd2;
                    end else begin
                        oq0  <= acc_sum;
                        ocnt <= 2'd1;
                    end
                end
                default: ;
            endcase
        end
    end

    assign out_valid = (ocnt != 2'd0);
    assign out_c = {{(ACC_W-ACC_BITS){oq0[ACC_BITS-1]}}, oq0};

endmodule