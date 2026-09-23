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

    // Constants
    localparam CHUNKS_PER_ROW = COLS / LANES;  // 4
    localparam A_BEATS = ROWS * CHUNKS_PER_ROW;  // 64

    // States
    typedef enum logic [2:0] {
        IDLE    = 3'd0,
        RECEIVE = 3'd1,
        COMPUTE = 3'd2,
        OUTPUT  = 3'd3,
        DONE    = 3'd4
    } state_t;
    state_t state;

    // Input counters
    logic [10:0] a_cnt;
    logic [5:0] x_cnt;

    // Output counter
    logic [7:0] out_cnt;

    // X register file: stores x[0..COLS-1]
    logic signed [DATA_W-1:0] x_reg [0:COLS-1];

    // Accumulators: one per row
    logic signed [ACC_W-1:0] acc [0:ROWS-1];

    // Compute multiplier results
    logic signed [ACC_W-1:0] prod [0:LANES-1];

    // Compute base address for x_reg
    logic [6:0] base;

    // Compute chunk and row indices
    logic [1:0] chunk;
    logic [3:0] row;

    // X register loading in RECEIVE state
    always_ff @(posedge clk) begin
        if (state == RECEIVE && in_x_flat_valid && in_a_flat_valid) begin
            for (int i = 0; i < LANES; i++) begin
                x_reg[x_cnt + i] <= in_x_flat[i*DATA_W +: DATA_W];
            end
        end
    end

    // Combinational compute logic: calculate base, chunk, row
    always_comb begin
        if (state == COMPUTE) begin
            row = a_cnt[8:5];    // a_cnt / 4
            chunk = a_cnt[5:4]; // (a_cnt % 16) / 4 = a_cnt % 4
            base = (row * CHUNKS_PER_ROW + chunk) * LANES;  // (row*4 + chunk) * 16
        end else begin
            row = 4'd0;
            chunk = 2'd0;
            base = 7'd0;
        end
    end

    // Combinational multiplier array
    always_comb begin
        for (int i = 0; i < LANES; i++) begin
            prod[i] = $signed(in_a_flat[i*DATA_W +: DATA_W]) * $signed(x_reg[base + i]);
        end
    end

    // Main state machine and datapath
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            a_cnt <= 11'd0;
            x_cnt <= 6'd0;
            for (int r = 0; r < ROWS; r++) begin
                acc[r] <= {ACC_W{1'b0}};
            end
            out_cnt <= 8'd0;
            out_valid <= 1'b0;
            out_c <= {ACC_W{1'b0}};
        end else begin
            case (state)
                IDLE: begin
                    a_cnt <= 11'd0;
                    x_cnt <= 6'd0;
                    out_valid <= 1'b0;
                    if (in_a_flat_valid && in_x_flat_valid) begin
                        state <= RECEIVE;
                    end
                end

                RECEIVE: begin
                    if (in_a_flat_valid && in_x_flat_valid) begin
                        a_cnt <= (a_cnt < COLS) ? (a_cnt + LANES) : a_cnt;
                        x_cnt <= (x_cnt < COLS) ? (x_cnt + LANES) : x_cnt;
                        if (x_cnt >= COLS - LANES && a_cnt >= COLS - LANES) begin
                            state <= COMPUTE;
                        end
                    end
                end

                COMPUTE: begin
                    for (int r = 0; r < ROWS; r++) begin
                        if (r == row) begin
                            if (a_cnt == 0) begin
                                acc[r] <= prod[r % LANES];
                            end else begin
                                acc[r] <= acc[r] + prod[r % LANES];
                            end
                        end
                    end
                    if (a_cnt < COLS - 1) begin
                        a_cnt <= a_cnt + LANES;
                    end else begin
                        a_cnt <= COLS;
                        state <= OUTPUT;
                        out_cnt <= 8'd0;
                        out_c <= acc[0];
                        out_valid <= 1'b1;
                    end
                end

                OUTPUT: begin
                    if (out_ready) begin
                        if (out_cnt < ROWS - 1) begin
                            out_cnt <= out_cnt + 1;
                            out_c <= acc[out_cnt + 1];
                        end else begin
                            state <= DONE;
                            out_valid <= 1'b0;
                        end
                    end
                end

                DONE: begin
                    state <= IDLE;
                end
            endcase
        end
    end

    // Ready signals: always accept in RECEIVE state
    assign in_a_flat_ready = (state == IDLE) || (state == RECEIVE);
    assign in_x_flat_ready = (state == IDLE) || (state == RECEIVE);

endmodule
