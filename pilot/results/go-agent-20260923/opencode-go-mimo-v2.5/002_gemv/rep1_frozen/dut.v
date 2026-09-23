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

    // x storage: 64 elements
    reg signed [DATA_W-1:0] x_mem [0:COLS-1];
    // A storage: 1024 elements
    reg signed [DATA_W-1:0] a_mem [0:ROWS*COLS-1];

    // Input counters
    reg [6:0] a_cnt;
    reg [2:0] x_cnt;
    reg a_done, x_done;

    // State: 0=accept, 1=compute, 2=output
    reg [1:0] state;

    // Compute phase
    reg [3:0] row_idx;
    reg [2:0] group_idx;
    reg [ACC_W-1:0] acc;

    // Input handshake
    assign in_a_flat_ready = (state == 0) && !a_done;
    assign in_x_flat_ready = (state == 0) && !x_done;

    // Compute: read A beat and x beat, multiply and accumulate
    wire [5:0] a_beat = {row_idx[3:0], group_idx[1:0]};
    wire [5:0] x_beat = {1'b0, group_idx[2:0]};

    // Sum of products for current beat (16 lanes)
    reg signed [ACC_W-1:0] beat_sum;
    integer i;
    always_comb begin
        beat_sum = 0;
        for (i = 0; i < LANES; i = i + 1) begin
            beat_sum = beat_sum + $signed(a_mem[a_beat * LANES + i]) * $signed(x_mem[x_beat * LANES + i]);
        end
    end

    // Row result register
    reg [ACC_W-1:0] row_result;
    reg row_valid;

    // Output
    assign out_valid = row_valid;
    assign out_c = row_result[ACC_W-1:0];

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_cnt <= 0;
            x_cnt <= 0;
            a_done <= 0;
            x_done <= 0;
            state <= 0;
            row_idx <= 0;
            group_idx <= 0;
            acc <= 0;
            row_valid <= 0;
            row_result <= 0;
        end else begin
            case (state)
                0: begin
                    // Accept A
                    if (in_a_flat_valid && in_a_flat_ready) begin
                        begin : wr_a
                            integer j;
                            for (j = 0; j < LANES; j = j + 1)
                                a_mem[a_cnt * LANES + j] <= in_a_flat[j*DATA_W +: DATA_W];
                        end
                        if (a_cnt == 7'd63) a_done <= 1;
                        a_cnt <= a_cnt + 1;
                    end
                    // Accept X
                    if (in_x_flat_valid && in_x_flat_ready) begin
                        begin : wr_x
                            integer j;
                            for (j = 0; j < LANES; j = j + 1)
                                x_mem[x_cnt * LANES + j] <= in_x_flat[j*DATA_W +: DATA_W];
                        end
                        if (x_cnt == 3'd3) x_done <= 1;
                        x_cnt <= x_cnt + 1;
                    end
                    if (a_done && x_done) begin
                        state <= 1;
                        row_idx <= 0;
                        group_idx <= 0;
                        acc <= 0;
                        row_valid <= 0;
                    end
                end
                1: begin
                    // Accumulate one group
                    if (group_idx == 0)
                        acc <= beat_sum;
                    else
                        acc <= acc + beat_sum;

                    if (group_idx == 3) begin
                        // Row done, capture result and go to output
                        row_result <= (group_idx == 0) ? beat_sum : acc + beat_sum;
                        row_valid <= 1;
                        state <= 2;
                    end else begin
                        group_idx <= group_idx + 1;
                    end
                end
                2: begin
                    // Wait for output handshake
                    if (out_ready) begin
                        row_valid <= 0;
                        if (row_idx == 4'd15) begin
                            // Transaction complete
                            state <= 0;
                            a_cnt <= 0;
                            x_cnt <= 0;
                            a_done <= 0;
                            x_done <= 0;
                            row_idx <= 0;
                        end else begin
                            state <= 1;
                            row_idx <= row_idx + 1;
                            group_idx <= 0;
                            acc <= 0;
                        end
                    end
                end
            endcase
        end
    end

endmodule
