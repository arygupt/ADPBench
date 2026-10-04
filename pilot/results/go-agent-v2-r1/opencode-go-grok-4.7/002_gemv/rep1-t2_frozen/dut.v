// Exact int8 GEMV: y[r] = sum_c A[r][c] * x[c], int32 result.
// A is row-major, LANES-wide beats. x is buffered and reused for every row.
// Independent valid/ready streams, backpressure, back-to-back transactions.

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

    localparam SEG_N   = COLS / LANES;
    localparam A_BEATS = ROWS * SEG_N;
    localparam SEG_W   = $clog2(SEG_N);
    localparam X_CW    = $clog2(SEG_N + 1);
    localparam A_CW    = $clog2(A_BEATS + 1);
    localparam O_CW    = $clog2(ROWS);
    localparam PROD_W  = 2 * DATA_W;   // 16: full int8 product
    localparam DOT_W   = 20;           // 16 * 16384 = 262144 fits
    localparam ACC_I   = 24;           // 4 * 262144 = 1048576 fits

    // Signed 8x8 -> 16. Bits 0..6 of b have positive weight; bit 7 is -128.
    // Prefix sums of the positive partial products fit in 16 bits, as does
    // the final subtraction, so the adder stays at PROD_W (not ACC_W).
    function automatic signed [PROD_W-1:0] mul_s;
        input [DATA_W-1:0] a;
        input [DATA_W-1:0] b;
        integer i;
        reg signed [PROD_W-1:0] a_sext;
        reg [PROD_W-1:0] pp;
        reg signed [PROD_W-1:0] sum;
        begin
            a_sext = {{DATA_W{a[DATA_W-1]}}, a};
            sum    = {PROD_W{1'b0}};
            for (i = 0; i < DATA_W - 1; i = i + 1) begin
                pp  = (a_sext <<< i) & {PROD_W{b[i]}};
                sum = sum + $signed(pp);
            end
            pp    = (a_sext <<< (DATA_W - 1)) & {PROD_W{b[DATA_W-1]}};
            sum   = sum - $signed(pp);
            mul_s = sum;
        end
    endfunction

    reg [LANES*DATA_W-1:0] x_mem [0:SEG_N-1];

    reg [X_CW-1:0]          x_recv;
    reg [A_CW-1:0]          a_recv;
    reg signed [ACC_I-1:0]  acc;
    reg signed [ACC_W-1:0]  out_c_r;
    reg                     out_valid_r;
    reg [O_CW-1:0]          out_count;

    wire [SEG_W-1:0] seg       = a_recv[SEG_W-1:0];
    wire             row_last  = (seg == SEG_N[SEG_W-1:0] - 1'b1);
    wire             out_busy  = out_valid_r && !out_ready;
    wire             x_full    = (x_recv == SEG_N[X_CW-1:0]);
    wire             a_done    = (a_recv == A_BEATS[A_CW-1:0]);
    wire             seg_ready = x_recv > {{(X_CW-SEG_W){1'b0}}, seg};

    assign in_x_flat_ready = !x_full;
    assign in_a_flat_ready = !a_done && seg_ready && (!row_last || !out_busy);

    wire x_fire   = in_x_flat_valid && in_x_flat_ready;
    wire a_fire   = in_a_flat_valid && in_a_flat_ready;
    wire out_fire = out_valid_r && out_ready;
    wire txn_done = out_fire && (out_count == ROWS[O_CW-1:0] - 1'b1);

    wire [LANES*DATA_W-1:0] x_beat = x_mem[seg];

    wire signed [PROD_W-1:0] prod [0:LANES-1];

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_mul
            assign prod[gi] = mul_s(
                in_a_flat[gi*DATA_W +: DATA_W],
                x_beat[gi*DATA_W +: DATA_W]
            );
        end
    endgenerate

    // Balanced tree, DOT_W bits. Depth 4 instead of a 32-bit chain.
    wire signed [DOT_W-1:0] l0 [0:LANES-1];
    wire signed [DOT_W-1:0] l1 [0:LANES/2-1];
    wire signed [DOT_W-1:0] l2 [0:LANES/4-1];
    wire signed [DOT_W-1:0] l3 [0:LANES/8-1];
    wire signed [DOT_W-1:0] l4;

    genvar ti;
    generate
        for (ti = 0; ti < LANES; ti = ti + 1) begin : g_l0
            assign l0[ti] = prod[ti];
        end
        for (ti = 0; ti < LANES/2; ti = ti + 1) begin : g_l1
            assign l1[ti] = l0[2*ti] + l0[2*ti+1];
        end
        for (ti = 0; ti < LANES/4; ti = ti + 1) begin : g_l2
            assign l2[ti] = l1[2*ti] + l1[2*ti+1];
        end
        for (ti = 0; ti < LANES/8; ti = ti + 1) begin : g_l3
            assign l3[ti] = l2[2*ti] + l2[2*ti+1];
        end
    endgenerate
    assign l4 = l3[0] + l3[1];

    wire signed [ACC_I-1:0] dot = l4;
    wire signed [ACC_I-1:0] acc_next =
        (seg == {SEG_W{1'b0}}) ? dot : (acc + dot);

    assign out_valid = out_valid_r;
    assign out_c     = out_c_r;

    always @(posedge clk) begin
        if (rst_n && x_fire)
            x_mem[x_recv[SEG_W-1:0]] <= in_x_flat;
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_recv      <= {X_CW{1'b0}};
            a_recv      <= {A_CW{1'b0}};
            acc         <= {ACC_I{1'b0}};
            out_c_r     <= {ACC_W{1'b0}};
            out_valid_r <= 1'b0;
            out_count   <= {O_CW{1'b0}};
        end else if (txn_done) begin
            x_recv      <= {X_CW{1'b0}};
            a_recv      <= {A_CW{1'b0}};
            acc         <= {ACC_I{1'b0}};
            out_valid_r <= 1'b0;
            out_count   <= {O_CW{1'b0}};
        end else begin
            if (x_fire)
                x_recv <= x_recv + 1'b1;
            if (a_fire) begin
                a_recv <= a_recv + 1'b1;
                acc    <= acc_next;
            end
            if (a_fire && row_last) begin
                // 24-bit signed value, sign-extended into the int32 port.
                out_c_r     <= acc_next;
                out_valid_r <= 1'b1;
            end else if (out_fire) begin
                out_valid_r <= 1'b0;
            end
            if (out_fire)
                out_count <= out_count + 1'b1;
        end
    end

endmodule
