// Serial baseline: capture a beat, then compare one signed element per cycle.
// Strict comparison preserves the lowest column index on ties.
module dut #(
    parameter ROWS = 8, COLS = 64, LANES = 8, DATA_W = 8, ACC_W = 32
)(
    input wire clk, rst_n,
    input wire [LANES*DATA_W-1:0] in_x_flat,
    input wire in_x_flat_valid,
    output wire in_x_flat_ready,
    output wire out_valid,
    input wire out_ready,
    output wire [ACC_W-1:0] out_c
);
    localparam BEATS = COLS / LANES;
    localparam BW = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam LW = (LANES > 1) ? $clog2(LANES) : 1;
    localparam IW = (COLS > 1) ? $clog2(COLS) : 1;
    localparam RECV = 2'd0, SCAN = 2'd1, SEND = 2'd2;
    localparam signed [DATA_W-1:0] MIN_VALUE = {1'b1, {(DATA_W-1){1'b0}}};
    reg [1:0] state;
    reg [BW-1:0] beat;
    reg [LW-1:0] lane;
    reg [LANES*DATA_W-1:0] held;
    reg signed [DATA_W-1:0] best;
    reg [IW-1:0] best_index;
    wire signed [DATA_W-1:0] value = held[lane*DATA_W +: DATA_W];

    assign in_x_flat_ready = (state == RECV);
    assign out_valid = (state == SEND);
    assign out_c = best_index;

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= RECV;
            beat <= 0;
            lane <= 0;
            best <= MIN_VALUE;
            best_index <= 0;
        end else begin
            case (state)
                RECV: if (in_x_flat_valid) begin
                    held <= in_x_flat;
                    lane <= 0;
                    state <= SCAN;
                end
                SCAN: begin
                    if (value > best) begin
                        best <= value;
                        best_index <= beat * LANES + lane;
                    end
                    if (lane == LANES - 1) begin
                        lane <= 0;
                        if (beat == BEATS - 1) begin
                            beat <= 0;
                            state <= SEND;
                        end else begin
                            beat <= beat + 1'b1;
                            state <= RECV;
                        end
                    end else lane <= lane + 1'b1;
                end
                SEND: if (out_ready) begin
                    best <= MIN_VALUE;
                    best_index <= 0;
                    state <= RECV;
                end
                default: state <= RECV;
            endcase
        end
    end
endmodule
