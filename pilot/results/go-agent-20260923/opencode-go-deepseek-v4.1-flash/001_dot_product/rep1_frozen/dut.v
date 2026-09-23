// 001_dot_product
// c = sum_i a[i]*b[i], signed int8 inputs, exact int32 result.
//
// Two independent valid/ready input streams (LEN elements each, LANES per
// beat) and one valid/ready output word per transaction. Two back-to-back
// transactions without an intervening reset.
//
// The two input streams are joined beat-for-beat: a beat is consumed only
// when both ports present one, so no beat storage or input multiplexers are
// needed.  Each consumed pair of beats is multiplied lane-wise and folded
// straight into the accumulator; the final beat of a transaction produces the
// output word in the very same cycle.

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

    localparam integer BEATS = LEN / LANES;           // beats per transaction
    localparam integer PW    = 2*DATA_W;              // exact product width
    localparam integer TW    = PW + $clog2(LANES);    // exact beat-sum width

    reg  [15:0]             a_cnt, b_cnt;
    reg  signed [ACC_W-1:0] acc;
    reg                     out_v;
    reg  signed [ACC_W-1:0] out_r;

    // ---- joined input handshake ----------------------------------------
    wire collect = (a_cnt < BEATS) && (b_cnt < BEATS);
    assign in_a_flat_ready = collect && in_b_flat_valid;
    assign in_b_flat_ready = collect && in_a_flat_valid;

    wire pair = collect && in_a_flat_valid && in_b_flat_valid;

    // ---- exact lane products -------------------------------------------
    wire signed [PW-1:0] prod [0:LANES-1];
    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_prod
            assign prod[gi] = $signed(in_a_flat[gi*DATA_W +: DATA_W])
                            * $signed(in_b_flat[gi*DATA_W +: DATA_W]);
        end
    endgenerate

    // ---- balanced adder tree over the lanes -----------------------------
    wire signed [TW-1:0] beat_sum;
    generate
        if (LANES == 1) begin : g_one
            assign beat_sum = {{(TW-PW){prod[0][PW-1]}}, prod[0]};
        end else begin : g_tree
            wire signed [TW-1:0] node [0:2*LANES-1];
            for (gi = 0; gi < LANES; gi = gi + 1) begin : g_leaf
                assign node[LANES+gi] = {{(TW-PW){prod[gi][PW-1]}}, prod[gi]};
            end
            for (gi = 1; gi < LANES; gi = gi + 1) begin : g_int
                assign node[gi] = node[2*gi] + node[2*gi+1];
            end
            assign beat_sum = node[1];
        end
    endgenerate

    wire signed [ACC_W-1:0] dot_now =
        acc + {{(ACC_W-TW){beat_sum[TW-1]}}, beat_sum};

    wire [15:0] a_cnt_n  = a_cnt + (pair ? 16'd1 : 16'd0);
    wire [15:0] b_cnt_n  = b_cnt + (pair ? 16'd1 : 16'd0);
    wire        last_pair = pair && (a_cnt_n == BEATS) && (b_cnt_n == BEATS);

    // the final beat retires the output in the same cycle
    assign out_valid = out_v | last_pair;
    assign out_c     = last_pair ? dot_now : out_r;

    // ---- sequential -----------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_cnt <= 16'd0;
            b_cnt <= 16'd0;
            acc   <= {ACC_W{1'b0}};
            out_v <= 1'b0;
            out_r <= {ACC_W{1'b0}};
        end else if ((out_v && out_ready) || (last_pair && out_ready)) begin
            // transaction done: clear all per-transaction state
            a_cnt <= 16'd0;
            b_cnt <= 16'd0;
            acc   <= {ACC_W{1'b0}};
            out_v <= 1'b0;
        end else if (last_pair) begin
            a_cnt <= a_cnt_n;
            b_cnt <= b_cnt_n;
            acc   <= {ACC_W{1'b0}};
            out_v <= 1'b1;
            out_r <= dot_now;
        end else if (pair) begin
            a_cnt <= a_cnt_n;
            b_cnt <= b_cnt_n;
            acc   <= dot_now;
        end
    end

endmodule
