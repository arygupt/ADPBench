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

    localparam X_BEATS = IN_LEN / LANES;
    localparam OUT_POS = IN_LEN - K + 1;
    localparam LAST_STEP = OUT_POS + K - 1;

    // X storage: 8 beats, each 128 bits
    reg [LANES*DATA_W-1:0] x_beat [0:X_BEATS-1];

    // W storage: 4 filters, each 32 bits (4 weights)
    reg [K*DATA_W-1:0] w_filter [0:F-1];

    // Control
    reg computing;
    reg [3:0] x_beat_cnt;
    reg w_beat_done;
    reg [1:0] f_cnt;
    reg [7:0] step;

    // Window (no reset needed, overwritten before use)
    reg signed [DATA_W-1:0] xw0, xw1, xw2, xw3;

    // Input logic
    wire x_accept = in_x_flat_valid && in_x_flat_ready;
    wire w_accept = in_w_flat_valid && in_w_flat_ready;

    assign in_x_flat_ready = !computing && (x_beat_cnt < X_BEATS);
    assign in_w_flat_ready = !computing && !w_beat_done;

    wire x_next_done = (x_beat_cnt + x_accept) == X_BEATS;
    wire w_next_done = w_beat_done || w_accept;

    // X write
    genvar b;
    generate
        for (b = 0; b < X_BEATS; b = b + 1) begin : x_write
            always @(posedge clk) begin
                if (x_accept && (x_beat_cnt == b))
                    x_beat[b] <= in_x_flat;
            end
        end
    endgenerate

    // W write
    always @(posedge clk) begin
        if (w_accept) begin
            w_filter[0] <= in_w_flat[0*K*DATA_W +: K*DATA_W];
            w_filter[1] <= in_w_flat[1*K*DATA_W +: K*DATA_W];
            w_filter[2] <= in_w_flat[2*K*DATA_W +: K*DATA_W];
            w_filter[3] <= in_w_flat[3*K*DATA_W +: K*DATA_W];
        end
    end

    // Advance logic
    wire advance = computing && ((step < K) || out_ready);

    // Beat read mux
    reg [LANES*DATA_W-1:0] beat_data;
    always @(*) begin
        case (step[6:4])
            3'd0: beat_data = x_beat[0];
            3'd1: beat_data = x_beat[1];
            3'd2: beat_data = x_beat[2];
            3'd3: beat_data = x_beat[3];
            3'd4: beat_data = x_beat[4];
            3'd5: beat_data = x_beat[5];
            3'd6: beat_data = x_beat[6];
            default: beat_data = x_beat[7];
        endcase
    end

    wire signed [DATA_W-1:0] x_new = beat_data[step[3:0]*DATA_W +: DATA_W];

    // Weight read for current filter
    reg [K*DATA_W-1:0] curr_w;
    always @(*) begin
        case (f_cnt)
            2'd0: curr_w = w_filter[0];
            2'd1: curr_w = w_filter[1];
            2'd2: curr_w = w_filter[2];
            default: curr_w = w_filter[3];
        endcase
    end

    wire signed [DATA_W-1:0] w0 = curr_w[0*DATA_W +: DATA_W];
    wire signed [DATA_W-1:0] w1 = curr_w[1*DATA_W +: DATA_W];
    wire signed [DATA_W-1:0] w2 = curr_w[2*DATA_W +: DATA_W];
    wire signed [DATA_W-1:0] w3 = curr_w[3*DATA_W +: DATA_W];

    // Window shift (no reset)
    always @(posedge clk) begin
        if (advance && (step < IN_LEN)) begin
            xw0 <= xw1;
            xw1 <= xw2;
            xw2 <= xw3;
            xw3 <= x_new;
        end
    end

    // Control FSM
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            computing <= 1'b0;
            x_beat_cnt <= 0;
            w_beat_done <= 1'b0;
            f_cnt <= 0;
            step <= 0;
        end else begin
            if (!computing) begin
                if (x_accept)
                    x_beat_cnt <= x_beat_cnt + 1'b1;
                if (w_accept)
                    w_beat_done <= 1'b1;
                if (x_next_done && w_next_done) begin
                    computing <= 1'b1;
                    f_cnt <= 0;
                    step <= 0;
                end
            end else begin
                if (advance) begin
                    if (step == LAST_STEP) begin
                        if (f_cnt == F - 1) begin
                            computing <= 1'b0;
                            x_beat_cnt <= 0;
                            w_beat_done <= 1'b0;
                            f_cnt <= 0;
                            step <= 0;
                        end else begin
                            f_cnt <= f_cnt + 1'b1;
                            step <= 0;
                        end
                    end else begin
                        step <= step + 1'b1;
                    end
                end
            end
        end
    end

    // Output
    assign out_valid = computing && (step >= K);
    wire signed [ACC_W-1:0] p0 = xw0 * w0;
    wire signed [ACC_W-1:0] p1 = xw1 * w1;
    wire signed [ACC_W-1:0] p2 = xw2 * w2;
    wire signed [ACC_W-1:0] p3 = xw3 * w3;
    assign out_c = p0 + p1 + p2 + p3;

endmodule
