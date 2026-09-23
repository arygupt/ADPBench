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

    reg [LANES*DATA_W-1:0] xw [0:3];
    reg [1:0]  x_cnt;
    reg        x_loaded;

    reg [6:0]  a_cnt;
    wire [1:0] grp = a_cnt[1:0];

    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] out_buf;
    reg        out_pending;
    reg [3:0]  out_cnt;

    wire [DATA_W-1:0] ax [0:LANES-1];
    wire [DATA_W-1:0] xv [0:LANES-1];
    genvar j;
    generate
        for (j = 0; j < LANES; j = j + 1) begin : g_lane
            assign ax[j] = in_a_flat[j*DATA_W +: DATA_W];
            assign xv[j] = xw[grp][j*DATA_W +: DATA_W];
        end
    endgenerate

    wire signed [15:0] p [0:LANES-1];
    generate
        for (j = 0; j < LANES; j = j + 1) begin : g_prod
            assign p[j] = $signed(ax[j]) * $signed(xv[j]);
        end
    endgenerate

    wire signed [16:0] s1 [0:7];
    wire signed [17:0] s2 [0:3];
    wire signed [18:0] s3 [0:1];
    wire signed [19:0] s4;
    genvar k;
    generate
        for (k = 0; k < 8; k = k + 1) begin : g_t1
            assign s1[k] = $signed({p[2*k][15], p[2*k]}) + $signed({p[2*k+1][15], p[2*k+1]});
        end
        for (k = 0; k < 4; k = k + 1) begin : g_t2
            assign s2[k] = $signed({s1[2*k][16], s1[2*k]}) + $signed({s1[2*k+1][16], s1[2*k+1]});
        end
        for (k = 0; k < 2; k = k + 1) begin : g_t3
            assign s3[k] = $signed({s2[2*k][17], s2[2*k]}) + $signed({s2[2*k+1][17], s2[2*k+1]});
        end
        assign s4 = $signed({s3[0][18], s3[0]}) + $signed({s3[1][18], s3[1]});
    endgenerate

    wire signed [ACC_W-1:0] sum_ext = {{(ACC_W-20){s4[19]}}, s4};

    assign in_x_flat_ready = ~x_loaded;
    wire last_grp = (grp == 2'd3);
    wire a_go = x_loaded && (a_cnt < 7'd64) && (!last_grp || !out_pending);
    assign in_a_flat_ready = a_go;
    assign out_valid = out_pending;
    assign out_c = out_buf;

    wire x_fire = in_x_flat_valid && in_x_flat_ready;
    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire o_fire = out_valid && out_ready;

    wire last_out = (out_cnt == 4'd15);
    wire done = o_fire && last_out;

    wire new_row_out = a_fire && last_grp;
    wire pend_next = new_row_out ? 1'b1 : (o_fire ? 1'b0 : out_pending);

    reg [1:0] n_x_cnt;
    always @* begin
        n_x_cnt = x_cnt;
        if (x_fire)
            n_x_cnt = (x_cnt == 2'd3) ? 2'd0 : x_cnt + 2'd1;
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            x_cnt <= 2'd0;
            x_loaded <= 1'b0;
            a_cnt <= 7'd0;
            acc <= {ACC_W{1'b0}};
            out_pending <= 1'b0;
            out_cnt <= 4'd0;
        end else begin
            if (x_fire) begin
                xw[x_cnt] <= in_x_flat;
                if (x_cnt == 2'd3) x_loaded <= 1'b1;
            end
            x_cnt <= n_x_cnt;

            if (a_fire) begin
                if (last_grp) begin
                    acc <= {ACC_W{1'b0}};
                    out_buf <= acc + sum_ext;
                end else begin
                    acc <= acc + sum_ext;
                end
                a_cnt <= a_cnt + 7'd1;
            end

            out_pending <= pend_next;
            if (o_fire) out_cnt <= out_cnt + 4'd1;

            if (done) begin
                x_loaded <= 1'b0;
                x_cnt <= 2'd0;
                a_cnt <= 7'd0;
                acc <= {ACC_W{1'b0}};
                out_pending <= 1'b0;
                out_cnt <= 4'd0;
            end
        end
    end

endmodule
