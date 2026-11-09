// Full-frame serial baseline: buffer the image and scan K*K pixels for each
// output window, re-reading overlapping pixels. One signed compare per cycle.
module dut #(
    parameter HEIGHT = 16, WIDTH = 16, K = 3, LANES = 8, DATA_W = 8, ACC_W = 32
)(
    input wire clk, rst_n,
    input wire [LANES*DATA_W-1:0] in_x_flat,
    input wire in_x_flat_valid,
    output wire in_x_flat_ready,
    output wire out_valid,
    input wire out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    localparam BEATS = HEIGHT * WIDTH / LANES;
    localparam OUT_H = HEIGHT - K + 1, OUT_W = WIDTH - K + 1;
    localparam BW = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam RW = (HEIGHT > 1) ? $clog2(HEIGHT) : 1;
    localparam CW = (WIDTH > 1) ? $clog2(WIDTH) : 1;
    localparam TW = (K > 1) ? $clog2(K) : 1;
    localparam RECV = 2'd0, SCAN = 2'd1, SEND = 2'd2;
    localparam signed [DATA_W-1:0] MIN_VALUE = {1'b1, {(DATA_W-1){1'b0}}};
    reg [1:0] state;
    reg [BW-1:0] beat;
    reg [RW-1:0] out_row;
    reg [CW-1:0] out_col;
    reg [TW-1:0] tap_row, tap_col;
    reg signed [DATA_W-1:0] pixels [0:HEIGHT*WIDTH-1];
    reg signed [DATA_W-1:0] best;
    wire signed [DATA_W-1:0] value = pixels[(out_row+tap_row)*WIDTH+out_col+tap_col];
    assign in_x_flat_ready = (state == RECV);
    assign out_valid = (state == SEND);
    assign out_c = best;
    integer j;

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= RECV;
            beat <= 0;
            out_row <= 0;
            out_col <= 0;
            tap_row <= 0;
            tap_col <= 0;
            best <= MIN_VALUE;
        end else begin
            case (state)
                RECV: if (in_x_flat_valid) begin
                    for (j = 0; j < LANES; j = j + 1)
                        pixels[beat*LANES+j] <= in_x_flat[j*DATA_W +: DATA_W];
                    if (beat == BEATS - 1) begin
                        beat <= 0;
                        state <= SCAN;
                    end else beat <= beat + 1'b1;
                end
                SCAN: begin
                    if (value > best) best <= value;
                    if (tap_col == K - 1) begin
                        tap_col <= 0;
                        if (tap_row == K - 1) begin
                            tap_row <= 0;
                            state <= SEND;
                        end else tap_row <= tap_row + 1'b1;
                    end else tap_col <= tap_col + 1'b1;
                end
                SEND: if (out_ready) begin
                    best <= MIN_VALUE;
                    if (out_col == OUT_W - 1) begin
                        out_col <= 0;
                        if (out_row == OUT_H - 1) begin
                            out_row <= 0;
                            state <= RECV;
                        end else begin
                            out_row <= out_row + 1'b1;
                            state <= SCAN;
                        end
                    end else begin
                        out_col <= out_col + 1'b1;
                        state <= SCAN;
                    end
                end
                default: state <= RECV;
            endcase
        end
    end
endmodule
