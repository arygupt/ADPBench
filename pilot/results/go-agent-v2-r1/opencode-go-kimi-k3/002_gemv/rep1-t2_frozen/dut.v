// GEMV: y = A @ x, A is ROWS x COLS int8 row-major, x is COLS int8.
// Each A beat carries LANES elements; beat j holds A[j/4][ (j%4)*16 +: 16 ].
// Each x beat k holds x[k*16 +: 16].  One output word per row.
//
// Strategy: buffer x (4 beats).  For each accepted A beat, compute a 16-lane
// signed dot product against the matching x segment and accumulate.  Every
// 4 A beats completes one row; the row result goes into a single result
// register and is streamed out (rows complete every 4 cycles, the output
// drains in 1, so a 1-deep holding register sustains full throughput; under
// output backpressure the A stream stalls).  Absolute beat counters + guard
// comparators make back-to-back transactions work without an intervening
// reset and tolerate arbitrary relative stream timing.

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

    // Absolute counters; no per-transaction reset required.
    reg [11:0] a_total;   // A beats accepted:  [11:6]=period, [5:2]=row, [1:0]=segment
    reg [11:0] x_total;   // x beats accepted:  [11:2]=period, [1:0]=segment

    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] result_hold;
    reg                    pending;
    reg [COLS*DATA_W-1:0]  x_buf;    // 64 int8

    wire [1:0] a_seg = a_total[1:0];
    wire [1:0] x_seg = x_total[1:0];

    // A beat (period p, segment s) may be accepted once x segment s of period
    // p has been stored:  x_total > 4*p + s.
    wire [11:0] x_req_mark = {4'b0000, a_total[11:6], a_seg};
    wire        x_avail    = (x_total > x_req_mark);

    // A segment-3 beat publishes a row result; only accept it when the result
    // register is free (drained this cycle or earlier).
    wire        result_free = !pending || out_ready;

    assign in_a_flat_ready = x_avail && ((a_seg != 2'd3) || result_free);

    // Accepting x beat (period q, segment s) overwrites x_buf[s]; the previous
    // period's A stream needs that segment up to beat 64*(q-1)+60+s.
    wire [11:0] x_mark = {(x_total[7:2] - 6'd1), (6'd60 + {4'b0000, x_seg})};
    assign in_x_flat_ready = (x_total[11:2] == 10'd0) || (a_total > x_mark);

    // x segment matching the current A beat
    wire [LANES*DATA_W-1:0] x_lane = x_buf[a_seg*LANES*DATA_W +: LANES*DATA_W];

    // 16-lane signed dot product: true 8x8->16 multipliers + balanced tree.
    function automatic signed [ACC_W-1:0] dot_lanes;
        input [LANES*DATA_W-1:0] av;
        input [LANES*DATA_W-1:0] xv;
        integer k;
        reg signed [2*DATA_W-1:0]   p  [0:LANES-1];
        reg signed [2*DATA_W:0]     t8 [0:7];
        reg signed [2*DATA_W+1:0]   t4 [0:3];
        reg signed [2*DATA_W+2:0]   t2 [0:1];
        reg signed [2*DATA_W+3:0]   t1;
        begin
            for (k = 0; k < LANES; k = k + 1)
                p[k] = $signed(av[k*DATA_W +: DATA_W]) * $signed(xv[k*DATA_W +: DATA_W]);
            for (k = 0; k < 8; k = k + 1) t8[k] = p[2*k]   + p[2*k+1];
            for (k = 0; k < 4; k = k + 1) t4[k] = t8[2*k]  + t8[2*k+1];
            for (k = 0; k < 2; k = k + 1) t2[k] = t4[2*k]  + t4[2*k+1];
            t1 = t2[0] + t2[1];
            dot_lanes = t1;
        end
    endfunction

    wire signed [ACC_W-1:0] dot = dot_lanes(in_a_flat, x_lane);

    wire a_fire   = in_a_flat_valid && in_a_flat_ready;
    wire x_fire   = in_x_flat_valid && in_x_flat_ready;
    wire out_fire = pending && out_ready;
    wire publish  = a_fire && (a_seg == 2'd3);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_total     <= 12'd0;
            x_total     <= 12'd0;
            acc         <= {ACC_W{1'b0}};
            result_hold <= {ACC_W{1'b0}};
            pending     <= 1'b0;
        end else begin
            if (x_fire) begin
                x_buf[x_seg*LANES*DATA_W +: LANES*DATA_W] <= in_x_flat;
                x_total <= x_total + 12'd1;
            end
            if (a_fire) begin
                a_total <= a_total + 12'd1;
                acc     <= (a_seg == 2'd0) ? dot : (acc + dot);
            end
            if (publish) begin
                result_hold <= acc + dot;
                pending     <= 1'b1;
            end else if (out_fire) begin
                pending     <= 1'b0;
            end
        end
    end

    assign out_valid = pending;
    assign out_c     = result_hold;

endmodule
