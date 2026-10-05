// 1-D valid convolution: y[f][o] = sum_k x[o+k] * w[f][k]
// IN_LEN=128, F=4, K=4, OUT_POS=125, 500 outputs per transaction.
//
// Architecture:
//  - x samples live in a 1024-bit circular shift register "ring".
//    Receive: shift in LANES bytes per beat  (ring <= {beat, ring[1023:128]})
//             => after 8 beats ring[i] = x[i].
//    Compute: shift by 1 byte per accepted output
//             (ring <= {ring[7:0], ring[1023:8]}) so ring[0..3] = x[o..o+3].
//  - After OUT_POS=125 shifts per filter row, do K-1=3 extra shifts to
//    restore the alignment for the next row.
//  - 4 signed 8x8 multipliers + exact adder tree produce out_c combinationally.

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

    localparam OUT_POS   = IN_LEN - K + 1;    // 125
    localparam X_BEATS   = IN_LEN / LANES;    // 8
    localparam RBITS     = IN_LEN * DATA_W;   // 1024
    localparam LANE_BITS = LANES * DATA_W;    // 128
    localparam WBITS     = F * K * DATA_W;    // 128

    localparam S_RECV = 2'd0;
    localparam S_COMP = 2'd1;
    localparam S_REAL = 2'd2;

    reg [1:0]       state;
    reg [RBITS-1:0] ring;
    reg [WBITS-1:0] w_mem;
    reg [3:0]       x_beat;
    reg             w_done;
    reg [6:0]       o_cnt;
    reg [1:0]       f_cnt;
    reg [1:0]       real_cnt;

    wire x_fire = in_x_flat_valid & in_x_flat_ready;
    wire w_fire = in_w_flat_valid & in_w_flat_ready;

    assign in_x_flat_ready = (state == S_RECV) & (x_beat < X_BEATS[3:0]);
    assign in_w_flat_ready = (state == S_RECV) & ~w_done;
    assign out_valid       = (state == S_COMP);

    // tap samples for the current window
    wire signed [DATA_W-1:0] x0 = ring[DATA_W*0 +: DATA_W];
    wire signed [DATA_W-1:0] x1 = ring[DATA_W*1 +: DATA_W];
    wire signed [DATA_W-1:0] x2 = ring[DATA_W*2 +: DATA_W];
    wire signed [DATA_W-1:0] x3 = ring[DATA_W*3 +: DATA_W];

    // taps of the current filter row (row-major w[f][k] at f*K+k)
    wire [K*DATA_W-1:0] w_row = w_mem[f_cnt*K*DATA_W +: K*DATA_W];
    wire signed [DATA_W-1:0] w0 = w_row[DATA_W*0 +: DATA_W];
    wire signed [DATA_W-1:0] w1 = w_row[DATA_W*1 +: DATA_W];
    wire signed [DATA_W-1:0] w2 = w_row[DATA_W*2 +: DATA_W];
    wire signed [DATA_W-1:0] w3 = w_row[DATA_W*3 +: DATA_W];

    // exact signed products and sum
    wire signed [2*DATA_W-1:0] p0 = x0 * w0;
    wire signed [2*DATA_W-1:0] p1 = x1 * w1;
    wire signed [2*DATA_W-1:0] p2 = x2 * w2;
    wire signed [2*DATA_W-1:0] p3 = x3 * w3;

    wire signed [2*DATA_W:0]   s01 = {p0[2*DATA_W-1], p0} + {p1[2*DATA_W-1], p1};
    wire signed [2*DATA_W:0]   s23 = {p2[2*DATA_W-1], p2} + {p3[2*DATA_W-1], p3};
    wire signed [2*DATA_W+1:0] sum = {s01[2*DATA_W], s01} + {s23[2*DATA_W], s23};

    assign out_c = {{(ACC_W-2*DATA_W-2){sum[2*DATA_W+1]}}, sum};

    // shift by one byte (circular toward index 0)
    wire [RBITS-1:0] ring_sh1 = {ring[DATA_W-1:0], ring[RBITS-1:DATA_W]};
    // load: shift by LANES bytes, new beat enters at the top
    wire [RBITS-1:0] ring_ld  = {in_x_flat, ring[RBITS-1:LANE_BITS]};

    // datapath state (always written before read; no reset required)
    always @(posedge clk) begin
        if (state == S_RECV) begin
            if (x_fire) ring  <= ring_ld;
            if (w_fire) w_mem <= in_w_flat;
        end else if (state == S_COMP) begin
            if (out_ready) ring <= ring_sh1;
        end else begin // S_REAL
            ring <= ring_sh1;
        end
    end

    // control
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state    <= S_RECV;
            x_beat   <= 4'd0;
            w_done   <= 1'b0;
            o_cnt    <= 7'd0;
            f_cnt    <= 2'd0;
            real_cnt <= 2'd0;
        end else begin
            case (state)
                S_RECV: begin
                    if (x_fire) x_beat <= x_beat + 4'd1;
                    if (w_fire) w_done <= 1'b1;
                    if (((x_beat + (x_fire ? 4'd1 : 4'd0)) == X_BEATS[3:0]) &&
                        (w_done || w_fire)) begin
                        state <= S_COMP;
                        o_cnt <= 7'd0;
                        f_cnt <= 2'd0;
                    end
                end
                S_COMP: begin
                    if (out_ready) begin
                        if (o_cnt == OUT_POS-1) begin
                            o_cnt <= 7'd0;
                            if (f_cnt == F-1) begin
                                state  <= S_RECV;   // next transaction
                                x_beat <= 4'd0;
                                w_done <= 1'b0;
                                f_cnt  <= 2'd0;
                            end else begin
                                f_cnt    <= f_cnt + 2'd1;
                                real_cnt <= K-2;    // (K-1) realign shifts
                                state    <= S_REAL;
                            end
                        end else begin
                            o_cnt <= o_cnt + 7'd1;
                        end
                    end
                end
                default: begin // S_REAL
                    if (real_cnt == 2'd0) state <= S_COMP;
                    else                  real_cnt <= real_cnt - 2'd1;
                end
            endcase
        end
    end

endmodule
