module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
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

    // ---------------------------------------------------------------
    // Local parameters
    // ---------------------------------------------------------------
    localparam integer PROD_W = 2*DATA_W;              // 16
    localparam integer BPB    = COLS/LANES;            // 4 A beats per row
    localparam integer XBEATS = COLS/LANES;            // 4 x beats per txn
    localparam integer ABEATS = (ROWS*COLS)/LANES;     // 64 A beats per txn
    localparam integer ABW    = $clog2(ABEATS);        // 6
    localparam integer RPW    = $clog2(BPB);           // 2
    localparam integer XW     = $clog2(XBEATS+1);      // 3 (holds 0..4)
    localparam integer RW     = $clog2(ROWS);          // 4

    // adder-tree level sizes / widths (LANES = 16 -> 4 levels)
    localparam integer L1 = LANES/2;
    localparam integer W1 = PROD_W+1;
    localparam integer L2 = L1/2;
    localparam integer W2 = W1+1;
    localparam integer L3 = L2/2;
    localparam integer W3 = W2+1;
    localparam integer L4 = L3/2;
    localparam integer W4 = W3+1;
    localparam integer SUM_W = W4;                     // 20

    // ---------------------------------------------------------------
    // State
    // ---------------------------------------------------------------
    reg  [LANES*DATA_W-1:0] x_bank [0:BPB-1];   // buffered x vector
    reg  [XW-1:0]           x_count;            // x beats received this txn (0..XBEATS)
    reg  [ABW-1:0]          a_count;            // A beats accepted this txn (0..ABEATS-1)
    reg  signed [ACC_W-1:0] acc;                // row accumulator
    reg  [ACC_W-1:0]        res_mem [0:ROWS-1]; // result buffer
    reg  [RW:0]             wr_ptr;             // result write pointer (index + wrap)
    reg  [RW:0]             rd_ptr;             // result read pointer (index + wrap)

    wire [RPW-1:0] rp = a_count[RPW-1:0];       // beat-in-row index / x bank index

    // ---------------------------------------------------------------
    // x bank select (bank rp feeds the multipliers)
    // ---------------------------------------------------------------
    reg [LANES*DATA_W-1:0] x_sel_r;
    integer bi;
    always @* begin
        x_sel_r = x_bank[0];
        for (bi = 1; bi < BPB; bi = bi + 1) begin
            if (rp == bi) x_sel_r = x_bank[bi];
        end
    end
    wire [LANES*DATA_W-1:0] x_sel = x_sel_r;

    // ---------------------------------------------------------------
    // Parallel signed multipliers and adder tree
    // ---------------------------------------------------------------
    wire signed [LANES*PROD_W-1:0] prod;
    wire signed [L1*W1-1:0] t1;
    wire signed [L2*W2-1:0] t2;
    wire signed [L3*W3-1:0] t3;
    wire signed [L4*W4-1:0] t4;

    generate
        genvar gi;
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_prod
            assign prod[gi*PROD_W +: PROD_W] =
                $signed(in_a_flat[gi*DATA_W +: DATA_W]) *
                $signed(x_sel[gi*DATA_W +: DATA_W]);
        end
        for (gi = 0; gi < L1; gi = gi + 1) begin : g_t1
            assign t1[gi*W1 +: W1] =
                $signed(prod[(2*gi)*PROD_W +: PROD_W]) +
                $signed(prod[(2*gi+1)*PROD_W +: PROD_W]);
        end
        for (gi = 0; gi < L2; gi = gi + 1) begin : g_t2
            assign t2[gi*W2 +: W2] =
                $signed(t1[(2*gi)*W1 +: W1]) +
                $signed(t1[(2*gi+1)*W1 +: W1]);
        end
        for (gi = 0; gi < L3; gi = gi + 1) begin : g_t3
            assign t3[gi*W3 +: W3] =
                $signed(t2[(2*gi)*W2 +: W2]) +
                $signed(t2[(2*gi+1)*W2 +: W2]);
        end
        for (gi = 0; gi < L4; gi = gi + 1) begin : g_t4
            assign t4[gi*W4 +: W4] =
                $signed(t3[(2*gi)*W3 +: W3]) +
                $signed(t3[(2*gi+1)*W3 +: W3]);
        end
    endgenerate

    wire signed [ACC_W-1:0] psum_ext = {{(ACC_W-SUM_W){t4[SUM_W-1]}}, t4[SUM_W-1:0]};

    // ---------------------------------------------------------------
    // Row accumulation
    // ---------------------------------------------------------------
    wire row_first = (rp == 0);
    wire row_last  = (rp == BPB-1);
    wire txn_last  = row_last && (a_count == ABEATS-1);
    wire signed [ACC_W-1:0] acc_next = row_first ? psum_ext : (acc + psum_ext);

    // ---------------------------------------------------------------
    // Handshakes
    // ---------------------------------------------------------------
    wire res_full  = (wr_ptr[RW] != rd_ptr[RW]) && (wr_ptr[RW-1:0] == rd_ptr[RW-1:0]);
    wire res_empty = (wr_ptr == rd_ptr);

    wire bank_ready = (x_count > {{(XW-RPW){1'b0}}, rp});

    assign in_a_flat_ready = rst_n && bank_ready && !res_full;
    assign in_x_flat_ready = rst_n && (x_count < XBEATS);

    assign out_valid = rst_n && !res_empty;
    assign out_c     = res_mem[rd_ptr[RW-1:0]];

    wire a_accept = in_a_flat_valid && in_a_flat_ready;
    wire x_accept = in_x_flat_valid && in_x_flat_ready;
    wire o_accept = out_valid && out_ready;

    // ---------------------------------------------------------------
    // Sequential logic
    // ---------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_count <= {XW{1'b0}};
            a_count <= {ABW{1'b0}};
            acc     <= {ACC_W{1'b0}};
            wr_ptr  <= {(RW+1){1'b0}};
            rd_ptr  <= {(RW+1){1'b0}};
        end else begin
            if (x_accept) begin
                x_bank[x_count[RPW-1:0]] <= in_x_flat;
                x_count <= x_count + 1'b1;
            end
            if (a_accept) begin
                acc <= acc_next;
                if (row_last) begin
                    res_mem[wr_ptr[RW-1:0]] <= acc_next;
                    wr_ptr  <= wr_ptr + 1'b1;
                end
                if (txn_last) begin
                    a_count <= {ABW{1'b0}};
                    x_count <= {XW{1'b0}};
                end else begin
                    a_count <= a_count + 1'b1;
                end
            end
            if (o_accept) begin
                rd_ptr <= rd_ptr + 1'b1;
            end
        end
    end

endmodule
