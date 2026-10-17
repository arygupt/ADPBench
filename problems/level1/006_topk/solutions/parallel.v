// Streaming insertion: retain only K sorted values, with K compare/exchange
// stages in parallel hardware. Process one element per cycle from each beat.
module dut #(
    parameter LEN = 64, K = 8, LANES = 8, DATA_W = 8, ACC_W = 32
)(
    input wire clk, rst_n,
    input wire [LANES*DATA_W-1:0] in_x_flat,
    input wire in_x_flat_valid,
    output wire in_x_flat_ready,
    output wire out_valid,
    input wire out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    localparam BEATS = LEN / LANES;
    localparam BW = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam LW = (LANES > 1) ? $clog2(LANES) : 1;
    localparam KW = (K > 1) ? $clog2(K) : 1;
    localparam RECV = 2'd0, INSERT = 2'd1, SEND = 2'd2;
    localparam signed [DATA_W-1:0] MIN_VALUE = {1'b1, {(DATA_W-1){1'b0}}};
    reg [1:0] state;
    reg [BW-1:0] beat;
    reg [LW-1:0] lane;
    reg [KW-1:0] emitted;
    reg [LANES*DATA_W-1:0] held;
    reg signed [DATA_W-1:0] top_values [0:K-1];
    wire signed [DATA_W-1:0] carry [0:K];
    wire signed [DATA_W-1:0] updated [0:K-1];
    assign carry[0] = held[lane*DATA_W +: DATA_W];
    genvar n;
    generate
        for (n = 0; n < K; n = n + 1) begin: insertion
            wire swap = carry[n] > top_values[n];
            assign updated[n] = swap ? carry[n] : top_values[n];
            assign carry[n+1] = swap ? top_values[n] : carry[n];
        end
    endgenerate

    assign in_x_flat_ready = (state == RECV);
    assign out_valid = (state == SEND);
    assign out_c = top_values[emitted];
    integer j;
    always @(posedge clk) begin
        if (!rst_n) begin
            state <= RECV;
            beat <= 0;
            lane <= 0;
            emitted <= 0;
            for (j = 0; j < K; j = j + 1) top_values[j] <= MIN_VALUE;
        end else begin
            case (state)
                RECV: if (in_x_flat_valid) begin
                    held <= in_x_flat;
                    lane <= 0;
                    state <= INSERT;
                end
                INSERT: begin
                    for (j = 0; j < K; j = j + 1) top_values[j] <= updated[j];
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
                    if (emitted == K - 1) begin
                        emitted <= 0;
                        for (j = 0; j < K; j = j + 1) top_values[j] <= MIN_VALUE;
                        state <= RECV;
                    end else emitted <= emitted + 1'b1;
                end
                default: state <= RECV;
            endcase
        end
    end
endmodule
