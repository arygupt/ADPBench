// Serial baseline: one read/modify/write of a bin counter per input element.
module dut #(
    parameter LEN = 256, BINS = 16, LANES = 8, DATA_W = 8, ACC_W = 32
)(
    input wire clk, rst_n,
    input wire [LANES*DATA_W-1:0] in_x_flat,
    input wire in_x_flat_valid,
    output wire in_x_flat_ready,
    output wire out_valid,
    input wire out_ready,
    output wire [ACC_W-1:0] out_c
);
    localparam BEATS = LEN / LANES;
    localparam BW = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam LW = (LANES > 1) ? $clog2(LANES) : 1;
    localparam IW = (BINS > 1) ? $clog2(BINS) : 1;
    localparam CW = $clog2(LEN + 1);
    localparam RECV = 2'd0, COUNT = 2'd1, SEND = 2'd2;
    reg [1:0] state;
    reg [BW-1:0] beat;
    reg [LW-1:0] lane;
    reg [IW-1:0] emitted;
    reg [LANES*DATA_W-1:0] held;
    reg [CW-1:0] counts [0:BINS-1];
    // The task guarantees that every input ID is less than BINS.
    wire [IW-1:0] bin_id = held[lane*DATA_W +: DATA_W];
    assign in_x_flat_ready = (state == RECV);
    assign out_valid = (state == SEND);
    assign out_c = counts[emitted];
    integer b;

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= RECV;
            beat <= 0;
            lane <= 0;
            emitted <= 0;
            for (b = 0; b < BINS; b = b + 1) counts[b] <= 0;
        end else begin
            case (state)
                RECV: if (in_x_flat_valid) begin
                    held <= in_x_flat;
                    lane <= 0;
                    state <= COUNT;
                end
                COUNT: begin
                    counts[bin_id] <= counts[bin_id] + 1'b1;
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
                    if (emitted == BINS - 1) begin
                        emitted <= 0;
                        for (b = 0; b < BINS; b = b + 1) counts[b] <= 0;
                        state <= RECV;
                    end else emitted <= emitted + 1'b1;
                end
                default: state <= RECV;
            endcase
        end
    end
endmodule
