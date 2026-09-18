// Baseline: strictly serial dot product.
// One multiplier, one adder, one accumulator. MACs LANES elements per accepted beat.
// This is the "obviously correct, obviously slow" reference the score divides by.

module dut #(
    parameter LEN    = 256,
    parameter LANES  = 32,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,

    input  wire                    in_valid,
    output wire                    in_ready,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire [LANES*DATA_W-1:0] in_b_flat,

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

    assign in_ready  = (state == S_IDLE);
    assign out_valid = (state == S_DONE);
    assign out_c     = acc;

    wire signed [DATA_W-1:0]   a_el = a_hold[lane*DATA_W +: DATA_W];
    wire signed [DATA_W-1:0]   b_el = b_hold[lane*DATA_W +: DATA_W];
    wire signed [2*DATA_W-1:0] prod = a_el * b_el;

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= S_IDLE;
            beat  <= {BEAT_W{1'b0}};
            lane  <= {LANE_W{1'b0}};
            acc   <= {ACC_W{1'b0}};
        end else begin
            case (state)
                S_IDLE: begin
                    if (in_valid) begin
                        a_hold <= in_a_flat;
                        b_hold <= in_b_flat;
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
                    if (1'b1) begin
                        acc   <= {ACC_W{1'b0}};
                        beat  <= {BEAT_W{1'b0}};
                        state <= S_IDLE;
                    end
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule
