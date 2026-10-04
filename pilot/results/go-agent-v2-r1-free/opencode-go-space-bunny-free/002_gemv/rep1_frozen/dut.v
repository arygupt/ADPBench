// GEMV: y = A (ROWS x COLS, row-major, int8) @ x (COLS, int8) -> int32
//
// Beat mapping: A arrives as ROWS*COLS/LANES beats.  With GROUPS =
// COLS/LANES beats per row, beat index b maps to row b/GROUPS and column
// group b%GROUPS, and lane j of the beat is column (b%GROUPS)*LANES + j.
// x arrives as GROUPS beats; x beat g holds exactly the LANES values that
// every A beat of column group g needs.  So one A beat produces LANES
// products that all belong to a single row: LANES parallel signed 8x8
// multipliers feed an adder tree and one accumulator per row.
//
// Flow control: x beats are buffered in a register file that is filled once
// per transaction and released only after the last A beat of that
// transaction is accepted, so back-to-back transactions never mix data.
// An A beat is only accepted when its column group is already buffered, and
// the A stream is held while a pending output word is not accepted.

module dut #(
    parameter ROWS   = 16,
    parameter COLS   = 64,
    parameter LANES  = 16,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam GROUPS = COLS / LANES;                 // x beats per row (4)
    localparam ABEATS = (ROWS * COLS) / LANES;        // A beats per txn (64)
    localparam PW     = 2 * DATA_W;                   // product width (16)
    localparam W1     = PW + 1;                       // 17
    localparam W2     = W1 + 1;                       // 18
    localparam W3     = W2 + 1;                       // 19
    localparam W4     = W3 + 1;                       // 20
    localparam AW     = $clog2(COLS) + 2 * DATA_W;    // 22

    localparam [2:0] GFULL = GROUPS;

    reg  [GROUPS*LANES*DATA_W-1:0] xmem;
    reg  [2:0]                     xfill;
    reg  [5:0]                     acnt;
    reg  signed [AW-1:0]           acc;
    reg  [ACC_W-1:0]               outc;
    reg                             ov;

    wire [1:0] g       = acnt[1:0];
    wire       out_blk = ov & ~out_ready;
    wire       slot_ok = (xfill > {1'b0, g});

    assign in_a_flat_ready = ~out_blk & slot_ok;
    wire a_fire = in_a_flat_valid & in_a_flat_ready;

    assign in_x_flat_ready = (xfill < GFULL);
    wire x_fire = in_x_flat_valid & in_x_flat_ready;

    assign out_valid = ov;
    assign out_c     = outc;

    // ------------------------------------------------------------------
    // datapath: LANES signed 8x8 products -> adder tree -> accumulator
    // ------------------------------------------------------------------
    wire [LANES*DATA_W-1:0] xs = xmem[g*LANES*DATA_W +: LANES*DATA_W];

    wire signed [PW-1:0] prod [0:LANES-1];
    wire signed [W1-1:0] s1   [0:LANES/2-1];
    wire signed [W2-1:0] s2   [0:LANES/4-1];
    wire signed [W3-1:0] s3   [0:LANES/8-1];
    wire signed [W4-1:0] s4;

    genvar i;
    generate
        for (i = 0; i < LANES; i = i + 1) begin : gmul
            wire signed [DATA_W-1:0] a8;
            wire signed [DATA_W-1:0] x8;
            wire signed [PW-1:0]     av;
            wire signed [PW-1:0]     xv;
            assign a8 = in_a_flat[i*DATA_W +: DATA_W];
            assign x8 = xs[i*DATA_W +: DATA_W];
            assign av = {{(PW-DATA_W){a8[DATA_W-1]}}, a8};
            assign xv = {{(PW-DATA_W){x8[DATA_W-1]}}, x8};
            assign prod[i] = av * xv;
        end

        for (i = 0; i < LANES/2; i = i + 1) begin : g1
            assign s1[i] = prod[2*i] + prod[2*i+1];
        end
        for (i = 0; i < LANES/4; i = i + 1) begin : g2
            assign s2[i] = s1[2*i] + s1[2*i+1];
        end
        for (i = 0; i < LANES/8; i = i + 1) begin : g3
            assign s3[i] = s2[2*i] + s2[2*i+1];
        end
        assign s4 = s3[0] + s3[1];
    endgenerate

    wire signed [AW-1:0] tree  = {{(AW-W4){s4[W4-1]}}, s4};
    wire signed [AW-1:0] accsum = acc + tree;

    // ------------------------------------------------------------------
    // control / storage
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            xmem  <= {GROUPS*LANES*DATA_W{1'b0}};
            xfill <= 3'd0;
            acnt  <= 6'd0;
            acc   <= {AW{1'b0}};
            outc  <= {ACC_W{1'b0}};
            ov    <= 1'b0;
        end else begin
            if (a_fire) begin
                acnt <= acnt + 6'd1;
                acc  <= (g == 2'd3) ? {AW{1'b0}} : accsum;
                if (g == 2'd3) begin
                    outc <= {{(ACC_W-AW){accsum[AW-1]}}, accsum};
                    ov   <= 1'b1;
                end else if (ov & out_ready) begin
                    ov <= 1'b0;
                end
                if (acnt == ABEATS-1) begin
                    xfill <= 3'd0;   // buffer free for next transaction's x
                end
            end else if (ov & out_ready) begin
                ov <= 1'b0;
            end

            if (x_fire) begin
                xmem[xfill[1:0]*LANES*DATA_W +: LANES*DATA_W] <= in_x_flat;
                xfill <= xfill + 3'd1;
            end
        end
    end

endmodule