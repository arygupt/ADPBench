module dut #(
    parameter ROWS  = 16,
    parameter COLS  = 64,
    parameter LANES = 16,
    parameter DATA_W= 8,
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

    // x vector storage (4 beats of 16 lanes)
    reg [1:0]  x_cnt;
    reg        x_done;
    reg signed [7:0] xs [0:63];

    // A beat counter within transaction (0..64), output counter (0..15)
    reg [6:0]  a_cnt;
    reg [3:0]  out_cnt;
    reg signed [19:0] acc [0:15];

    // transaction ends when last output word accepted
    wire txn_end = out_valid && out_ready && (out_cnt == 4'd15);

    assign in_x_flat_ready = !x_done && !txn_end;
    assign in_a_flat_ready = x_done  && !txn_end;

    wire x_fire = in_x_flat_valid && in_x_flat_ready;
    wire a_fire = in_a_flat_valid && in_a_flat_ready;

    // current a beat decode: element e = 16*beat + lane
    // row = beat/4, column block = beat%4, lane j
    wire [1:0] colblk = a_cnt[1:0];
    wire [4:0] row    = a_cnt[6:2];

    wire [6:0] need = {3'b000, out_cnt, 2'b00} + 7'd4; // (out_cnt+1)*4
    assign out_valid = (a_cnt >= need);

    // lane products
    genvar g;
    wire signed [7:0]  av [0:15];
    wire signed [15:0] pr [0:15];
    generate
        for (g = 0; g < 16; g = g + 1) begin : lane
            assign av[g] = in_a_flat[g*DATA_W +: DATA_W];
            assign pr[g] = av[g] * xs[{colblk, 4'd0} + g[3:0]];
        end
    endgenerate

    // sum of 16 products, fits in 20 signed bits (|sum| <= 16*128*128 < 2^18)
    reg signed [19:0] psum;
    integer k;
    always @* begin
        psum = 20'sd0;
        for (k = 0; k < 16; k = k + 1)
            psum = psum + pr[k];
    end

    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_cnt  <= 2'd0;
            x_done <= 1'b0;
            a_cnt  <= 7'd0;
            out_cnt<= 4'd0;
            for (i = 0; i < 16; i = i + 1)
                acc[i] <= 20'sd0;
        end else if (txn_end) begin
            // clear per-transaction state for next transaction
            x_cnt  <= 2'd0;
            x_done <= 1'b0;
            a_cnt  <= 7'd0;
            out_cnt<= 4'd0;
        end else begin
            if (a_fire) begin
                acc[row[2:0]? 0:0] ; // placeholder removed below
            end
            if (x_fire) begin
                xs[x_cnt] <= in_x_flat[7:0];
                if (x_cnt == 2'd3) x_done <= 1'b1;
                x_cnt <= x_cnt + 2'd1;
            end
            if (out_valid && out_ready)
                out_cnt <= out_cnt + 4'd1;
        end
    end

    // correct accumulation (separate always for clarity of masking row index)
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // already cleared above-agnostic; keep acc cleared here too
            for (i = 0; i < 16; i = i + 1)
                acc[i] <= 20'sd0;
        end else if (a_fire) begin
            if (txn_end)
                acc[row] <= psum;
            else if (a_cnt[1:0] == 2'd0 && a_cnt[3:2] == 2'd0)
                acc[row] <= acc[row] + psum;
            else
                acc[row] <= acc[row] + psum;
        end
    end

    assign out_c = {{(ACC_W-20){acc[out_cnt][19]}}, acc[out_cnt]};

endmodule
