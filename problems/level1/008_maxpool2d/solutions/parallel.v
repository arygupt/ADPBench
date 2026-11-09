// Streaming 3x3 max: two line buffers and two horizontal maxima reuse pixels.
// K=3 is fixed by this task. Each pixel updates a vertical maximum, then the
// three adjacent vertical maxima produce the window maximum. Stall all state
// while an output is blocked. Old line-buffer contents are overwritten by the
// first two rows of each transaction before any output is emitted.
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
    localparam RW = (HEIGHT > 1) ? $clog2(HEIGHT) : 1;
    localparam CW = (WIDTH > 1) ? $clog2(WIDTH) : 1;
    localparam LW = (LANES > 1) ? $clog2(LANES) : 1;
    localparam RECV = 2'd0, PIXEL = 2'd1, SEND = 2'd2;
    reg [1:0] state, resume_state;
    reg [RW-1:0] row;
    reg [CW-1:0] col;
    reg [LW-1:0] lane;
    reg [LANES*DATA_W-1:0] held;
    reg signed [DATA_W-1:0] line1 [0:WIDTH-1];
    reg signed [DATA_W-1:0] line2 [0:WIDTH-1];
    reg signed [DATA_W-1:0] horizontal1, horizontal2, out_value;
    wire signed [DATA_W-1:0] pixel = held[lane*DATA_W +: DATA_W];
    wire signed [DATA_W-1:0] upper = line1[col] > line2[col] ? line1[col] : line2[col];
    wire signed [DATA_W-1:0] vertical = pixel > upper ? pixel : upper;
    wire signed [DATA_W-1:0] previous = horizontal1 > horizontal2 ? horizontal1 : horizontal2;
    wire signed [DATA_W-1:0] window_max = vertical > previous ? vertical : previous;
    wire [1:0] next_state = (lane == LANES - 1) ? RECV : PIXEL;
    assign in_x_flat_ready = (state == RECV);
    assign out_valid = (state == SEND);
    assign out_c = out_value;

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= RECV;
            resume_state <= RECV;
            row <= 0;
            col <= 0;
            lane <= 0;
            horizontal1 <= 0;
            horizontal2 <= 0;
            out_value <= 0;
        end else begin
            case (state)
                RECV: if (in_x_flat_valid) begin
                    held <= in_x_flat;
                    lane <= 0;
                    state <= PIXEL;
                end
                PIXEL: begin
                    line2[col] <= line1[col];
                    line1[col] <= pixel;
                    horizontal2 <= horizontal1;
                    horizontal1 <= vertical;
                    if (col == WIDTH - 1) begin
                        col <= 0;
                        if (row == HEIGHT - 1) row <= 0;
                        else row <= row + 1'b1;
                    end else col <= col + 1'b1;
                    if (lane == LANES - 1) lane <= 0;
                    else lane <= lane + 1'b1;
                    if (row >= K - 1 && col >= K - 1) begin
                        out_value <= window_max;
                        resume_state <= next_state;
                        state <= SEND;
                    end else state <= next_state;
                end
                SEND: if (out_ready) state <= resume_state;
                default: state <= RECV;
            endcase
        end
    end
endmodule
