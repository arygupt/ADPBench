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

    localparam integer BEATS_X = COLS / LANES;          // 4
    localparam integer BEATS_A = ROWS * COLS / LANES;   // 64
    localparam integer XCNT_W  = $clog2(BEATS_X + 1);  // 3
    localparam integer ACNT_W  = $clog2(BEATS_A + 1);  // 7
    localparam integer OCNT_W  = $clog2(ROWS + 1);     // 5

    reg [XCNT_W-1:0]       x_cnt_r;
    reg [ACNT_W-1:0]       a_cnt_r;
    reg [OCNT_W-1:0]       out_cnt_r;
    reg                    hold_r;
    reg signed [ACC_W-1:0] acc_r;
    reg signed [ACC_W-1:0] out_reg_r;
    reg                    out_valid_r;

    reg [LANES*DATA_W-1:0] x_r0, x_r1, x_r2, x_r3;

    // column-block (group) of the A beat currently being accepted; BEATS_X = 4
    wire [1:0] grp = a_cnt_r[1:0];

    // select the x beat matching the current group
    wire [LANES*DATA_W-1:0] x_cur;
    assign x_cur = (grp == 2'd0) ? x_r0 :
                   (grp == 2'd1) ? x_r1 :
                   (grp == 2'd2) ? x_r2 : x_r3;

    // 16 signed products
    wire signed [2*DATA_W-1:0] prod [0:LANES-1];
    genvar l;
    generate
        for (l = 0; l < LANES; l = l + 1) begin : g_prod
            assign prod[l] = $signed(in_a_flat[l*DATA_W +: DATA_W]) *
                             $signed(x_cur[l*DATA_W +: DATA_W]);
        end
    endgenerate

    // minimal-width adder tree
    wire signed [2*DATA_W:0]   s1 [0:LANES/2-1];   // 17-bit
    wire signed [2*DATA_W+1:0] s2 [0:LANES/4-1];   // 18-bit
    wire signed [2*DATA_W+2:0] s3 [0:LANES/8-1];   // 19-bit
    wire signed [2*DATA_W+3:0] tree_sum;           // 20-bit
    generate
        for (l = 0; l < LANES/2; l = l + 1) begin : g_s1
            assign s1[l] = {{1{prod[2*l][2*DATA_W-1]}}, prod[2*l]} +
                           {{1{prod[2*l+1][2*DATA_W-1]}}, prod[2*l+1]};
        end
        for (l = 0; l < LANES/4; l = l + 1) begin : g_s2
            assign s2[l] = {{1{s1[2*l][2*DATA_W]}}, s1[2*l]} +
                           {{1{s1[2*l+1][2*DATA_W]}}, s1[2*l+1]};
        end
        for (l = 0; l < LANES/8; l = l + 1) begin : g_s3
            assign s3[l] = {{1{s2[2*l][2*DATA_W+1]}}, s2[2*l]} +
                           {{1{s2[2*l+1][2*DATA_W+1]}}, s2[2*l+1]};
        end
    endgenerate
    assign tree_sum = {{1{s3[0][2*DATA_W+2]}}, s3[0]} +
                      {{1{s3[1][2*DATA_W+2]}}, s3[1]};

    wire signed [ACC_W-1:0] ext_sum  = {{(ACC_W-2*DATA_W-4){tree_sum[2*DATA_W+3]}}, tree_sum};
    wire signed [ACC_W-1:0] acc_next = (grp == 2'd0) ? ext_sum : (acc_r + ext_sum);

    wire x_accept   = in_x_flat_valid && in_x_flat_ready;
    wire a_accept   = in_a_flat_valid && in_a_flat_ready;
    wire out_accept = out_valid_r && out_ready;
    wire out_free   = !out_valid_r || out_ready;

    assign in_x_flat_ready = (x_cnt_r < BEATS_X);
    assign in_a_flat_ready = !hold_r && (a_cnt_r < BEATS_A) && (grp < x_cnt_r);
    assign out_valid = out_valid_r;
    assign out_c     = out_reg_r;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_cnt_r     <= {XCNT_W{1'b0}};
            a_cnt_r     <= {ACNT_W{1'b0}};
            out_cnt_r   <= {OCNT_W{1'b0}};
            hold_r      <= 1'b0;
            acc_r       <= {ACC_W{1'b0}};
            out_reg_r   <= {ACC_W{1'b0}};
            out_valid_r <= 1'b0;
        end else begin
            // accept x beats (4 per transaction)
            if (x_accept) begin
                case (x_cnt_r)
                    2'd0:    x_r0 <= in_x_flat;
                    2'd1:    x_r1 <= in_x_flat;
                    2'd2:    x_r2 <= in_x_flat;
                    default: x_r3 <= in_x_flat;
                endcase
                x_cnt_r <= x_cnt_r + 1'b1;
            end

            // output acceptance
            if (out_accept) begin
                if (out_cnt_r == ROWS - 1) begin
                    // last output of the transaction: finish and clear state
                    out_cnt_r   <= {OCNT_W{1'b0}};
                    a_cnt_r     <= {ACNT_W{1'b0}};
                    x_cnt_r     <= {XCNT_W{1'b0}};
                    hold_r      <= 1'b0;
                    acc_r       <= {ACC_W{1'b0}};
                    out_valid_r <= 1'b0;
                end else begin
                    out_cnt_r <= out_cnt_r + 1'b1;
                    if (hold_r) begin
                        out_reg_r   <= acc_r;
                        out_valid_r <= 1'b1;
                        acc_r       <= {ACC_W{1'b0}};
                        hold_r      <= 1'b0;
                    end else begin
                        out_valid_r <= 1'b0;
                    end
                end
            end

            // accept A beats and accumulate
            if (a_accept) begin
                a_cnt_r <= a_cnt_r + 1'b1;
                if (grp == 2'd3) begin
                    // last beat of a row: row result ready in acc_next
                    if (out_free) begin
                        out_reg_r   <= acc_next;
                        out_valid_r <= 1'b1;
                        acc_r       <= {ACC_W{1'b0}};
                    end else begin
                        hold_r      <= 1'b1;
                        acc_r       <= acc_next;
                    end
                end else begin
                    acc_r <= acc_next;
                end
            end
        end
    end

endmodule
