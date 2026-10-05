module dut #(
    parameter IN_LEN = 128,
    parameter F = 4,
    parameter K = 4,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_w_flat,
    input  wire                    in_w_flat_valid,
    output wire                    in_w_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    localparam integer OUT_POS = IN_LEN - K + 1;
    localparam integer X_BEATS = (IN_LEN + LANES - 1) / LANES;
    localparam integer W_ELEMS = F * K;
    localparam integer W_BEATS = (W_ELEMS + LANES - 1) / LANES;
    localparam integer POS_W = (OUT_POS <= 1) ? 1 : $clog2(OUT_POS);
    localparam integer FILT_W = (F <= 1) ? 1 : $clog2(F);

    localparam [1:0] ST_COLLECT = 2'd0;
    localparam [1:0] ST_INIT    = 2'd1;
    localparam [1:0] ST_OUTPUT  = 2'd2;

    reg [1:0] state;
    reg x_done, w_done;
    reg [31:0] x_beat_count, w_beat_count;
    reg [POS_W-1:0] out_pos;
    reg [FILT_W-1:0] out_filter;

    reg signed [DATA_W-1:0] x_mem [0:IN_LEN-1];
    reg signed [DATA_W-1:0] w_mem [0:W_ELEMS-1];
    reg signed [DATA_W-1:0] window [0:K-1];

    assign in_x_flat_ready = (state == ST_COLLECT) && !x_done;
    assign in_w_flat_ready = (state == ST_COLLECT) && !w_done;
    assign out_valid = (state == ST_OUTPUT);

    wire x_fire = in_x_flat_valid && in_x_flat_ready;
    wire w_fire = in_w_flat_valid && in_w_flat_ready;
    wire x_last_fire = x_fire && (x_beat_count == X_BEATS-1);
    wire w_last_fire = w_fire && (w_beat_count == W_BEATS-1);

    reg signed [ACC_W-1:0] sum_comb;
    reg signed [2*DATA_W-1:0] product_temp;
    integer k;
    always @* begin
        sum_comb = {ACC_W{1'b0}};
        product_temp = {2*DATA_W{1'b0}};
        for (k = 0; k < K; k = k + 1) begin
            product_temp =
                $signed({{DATA_W{window[k][DATA_W-1]}}, window[k]}) *
                $signed({{DATA_W{w_mem[out_filter*K+k][DATA_W-1]}}, w_mem[out_filter*K+k]});
            sum_comb = sum_comb +
                {{(ACC_W-2*DATA_W){product_temp[2*DATA_W-1]}}, product_temp};
        end
    end
    assign out_c = sum_comb;

    integer i;
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= ST_COLLECT;
            x_done <= 1'b0;
            w_done <= 1'b0;
            x_beat_count <= 0;
            w_beat_count <= 0;
            out_pos <= 0;
            out_filter <= 0;
            for (i = 0; i < K; i = i + 1)
                window[i] <= 0;
        end else begin
            case (state)
                ST_COLLECT: begin
                    if (x_fire) begin
                        for (i = 0; i < LANES; i = i + 1) begin
                            if (x_beat_count*LANES + i < IN_LEN)
                                x_mem[x_beat_count*LANES + i] <=
                                    $signed(in_x_flat[i*DATA_W +: DATA_W]);
                        end
                        if (x_last_fire)
                            x_done <= 1'b1;
                        else
                            x_beat_count <= x_beat_count + 1'b1;
                    end
                    if (w_fire) begin
                        for (i = 0; i < LANES; i = i + 1) begin
                            if (w_beat_count*LANES + i < W_ELEMS)
                                w_mem[w_beat_count*LANES + i] <=
                                    $signed(in_w_flat[i*DATA_W +: DATA_W]);
                        end
                        if (w_last_fire)
                            w_done <= 1'b1;
                        else
                            w_beat_count <= w_beat_count + 1'b1;
                    end
                    if ((x_done || x_last_fire) && (w_done || w_last_fire)) begin
                        state <= ST_INIT;
                        out_pos <= 0;
                        out_filter <= 0;
                    end
                end

                ST_INIT: begin
                    for (i = 0; i < K; i = i + 1)
                        window[i] <= x_mem[i];
                    state <= ST_OUTPUT;
                end

                ST_OUTPUT: begin
                    if (out_ready) begin
                        if (out_pos == OUT_POS-1) begin
                            out_pos <= 0;
                            if (out_filter == F-1) begin
                                state <= ST_COLLECT;
                                x_done <= 1'b0;
                                w_done <= 1'b0;
                                x_beat_count <= 0;
                                w_beat_count <= 0;
                                out_filter <= 0;
                            end else begin
                                out_filter <= out_filter + 1'b1;
                                for (i = 0; i < K; i = i + 1)
                                    window[i] <= x_mem[i];
                            end
                        end else begin
                            out_pos <= out_pos + 1'b1;
                            for (i = 0; i < K-1; i = i + 1)
                                window[i] <= window[i+1];
                            window[K-1] <= x_mem[out_pos + K];
                        end
                    end
                end

                default: begin
                    state <= ST_COLLECT;
                end
            endcase
        end
    end
endmodule
