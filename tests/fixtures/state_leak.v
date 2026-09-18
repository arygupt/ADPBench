// Baseline: strictly serial dot product.
// One multiplier, one adder, one accumulator. MACs LANES elements per captured
// beat, one element per cycle. Handles repeated transactions: the accumulator
// and beat counter are cleared when the output is accepted.
//
// Input streams are independent, so each port is captured into a hold register
// as it arrives; the core proceeds once both holds are full.

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

    localparam BEATS  = LEN / LANES;
    localparam LANE_W = $clog2(LANES);
    localparam BEAT_W = $clog2(BEATS);

    localparam S_IDLE = 2'd0, S_MAC = 2'd1, S_DONE = 2'd2;

    reg [1:0]              state;
    reg [BEAT_W-1:0]       beat;
    reg [LANE_W-1:0]       lane;
    reg signed [ACC_W-1:0] acc;
    reg [LANES*DATA_W-1:0] a_hold;
    reg [LANES*DATA_W-1:0] b_hold;
    reg                    a_have;
    reg                    b_have;

    assign in_a_flat_ready = (state == S_IDLE) && !a_have;
    assign in_b_flat_ready = (state == S_IDLE) && !b_have;
    assign out_valid       = (state == S_DONE);
    assign out_c           = acc;

    wire signed [DATA_W-1:0]   a_el = a_hold[lane*DATA_W +: DATA_W];
    wire signed [DATA_W-1:0]   b_el = b_hold[lane*DATA_W +: DATA_W];
    wire signed [2*DATA_W-1:0] prod = a_el * b_el;

    always @(posedge clk) begin
        if (!rst_n) begin
            state  <= S_IDLE;
            beat   <= {BEAT_W{1'b0}};
            lane   <= {LANE_W{1'b0}};
            acc    <= {ACC_W{1'b0}};
            a_have <= 1'b0;
            b_have <= 1'b0;
        end else begin
            case (state)
                S_IDLE: begin
                    if (!a_have && in_a_flat_valid && in_a_flat_ready) begin
                        a_hold <= in_a_flat;
                        a_have <= 1'b1;
                    end
                    if (!b_have && in_b_flat_valid && in_b_flat_ready) begin
                        b_hold <= in_b_flat;
                        b_have <= 1'b1;
                    end
                    if (a_have && b_have) begin
                        a_have <= 1'b0;
                        b_have <= 1'b0;
                        lane   <= {LANE_W{1'b0}};
                        state  <= S_MAC;
                    end
                end

                S_MAC: begin
                    acc <= acc + prod;
                    if (lane == LANES - 1) begin
                        lane <= {LANE_W{1'b0}};
                        if (beat == BEATS - 1) begin
                            state <= S_DONE;
                        end else begin
                            beat  <= beat + 1'b1;
                            state <= S_IDLE;
                        end
                    end else begin
                        lane <= lane + 1'b1;
                    end
                end

                S_DONE: begin
                    if (out_ready) begin
                        beat  <= {BEAT_W{1'b0}};
                        state <= S_IDLE;
                    end
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
