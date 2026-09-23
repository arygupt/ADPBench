// ADPBench submission. Replace the body; keep the module name and ports.
// Run ./check.sh for synthesis + simulation feedback.
// The testbench drives several back-to-back transactions without reset, and
// every input port has its own valid/ready handshake.

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
    localparam A_BEATS = ROWS * COLS / LANES;  // 64 beats for A
    localparam X_BEATS = COLS / LANES;         // 4 beats for x
    localparam X_BITS = LANES * DATA_W;        // 128 bits per beat

    // A buffer: 64 beats x 128 bits
    reg [X_BITS-1:0] a_buffer [0:A_BEATS-1];
    reg a_buffer_valid;

    // x buffer: 4 beats x 128 bits
    reg [X_BITS-1:0] x_buffer [0:X_BEATS-1];
    reg x_buffer_valid;

    // Counters for input beats
    reg [$clog2(A_BEATS+1)-1:0] a_cnt;
    reg [$clog2(X_BEATS+1)-1:0] x_cnt;

    // Compute state
    reg computing;
    reg [3:0] row;           // 0 to 15
    reg [2:0] col_group;     // 0 to 3 (4 groups of 16 cols)
    reg [4:0] dot_step;      // 0 to 16 (16 multiply-adds per group, plus final)

    // Accumulator
    reg signed [ACC_W-1:0] acc;

    // x buffer read
    wire [X_BITS-1:0] x_rdata = x_buffer[col_group];
    wire signed [DATA_W-1:0] x_val [0:LANES-1];
    wire signed [DATA_W-1:0] a_val [0:LANES-1];

    // Unpack x and a values
    genvar g;
    generate
        for (g = 0; g < LANES; g = g + 1) begin : unpack
            assign x_val[g] = x_rdata[g*DATA_W +: DATA_W];
            assign a_val[g] = a_buffer[row*X_BEATS + col_group][g*DATA_W +: DATA_W];
        end
    endgenerate

    // Compute products and sum for current step
    // We process 16 multiplies per cycle, one group at a time
    wire signed [ACC_W-1:0] products [0:LANES-1];
    wire signed [ACC_W-1:0] sum_products;

    generate
        for (g = 0; g < LANES; g = g + 1) begin : mult
            assign products[g] = a_val[g] * x_val[g];
        end
    endgenerate

    // Tree reduction for sum
    // 16 elements -> sum
    wire signed [ACC_W-1:0] level1 [0:7];
    wire signed [ACC_W-1:0] level2 [0:3];
    wire signed [ACC_W-1:0] level3 [0:1];
    
    generate
        for (g = 0; g < 8; g = g + 1) begin : l1
            assign level1[g] = products[g*2] + products[g*2+1];
        end
        for (g = 0; g < 4; g = g + 1) begin : l2
            assign level2[g] = level1[g*2] + level1[g*2+1];
        end
        for (g = 0; g < 2; g = g + 1) begin : l3
            assign level3[g] = level2[g*2] + level2[g*2+1];
        end
    endgenerate
    
    assign sum_products = level3[0] + level3[1];

    // Output logic
    reg out_valid_reg;
    reg signed [ACC_W-1:0] out_c_reg;

    assign out_valid = out_valid_reg;
    assign out_c = out_c_reg;

    // Ready signals - accept when not done with current transaction
    // A needs A_BEATS beats, x needs X_BEATS beats
    wire a_done = a_cnt >= A_BEATS;
    wire x_done = x_cnt >= X_BEATS;
    
    assign in_a_flat_ready = !a_done && !computing;
    assign in_x_flat_ready = !x_done && !computing;

    // State machine
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_cnt <= 0;
            x_cnt <= 0;
            a_buffer_valid <= 0;
            x_buffer_valid <= 0;
            computing <= 0;
            row <= 0;
            col_group <= 0;
            dot_step <= 0;
            acc <= 0;
            out_valid_reg <= 0;
            out_c_reg <= 0;
        end else begin
            if (!computing) begin
                // Accept inputs
                if (!a_done && in_a_flat_valid && in_a_flat_ready) begin
                    a_buffer[a_cnt] <= in_a_flat;
                    a_cnt <= a_cnt + 1;
                    if (a_cnt == A_BEATS - 1) begin
                        a_buffer_valid <= 1;
                    end
                end
                
                if (!x_done && in_x_flat_valid && in_x_flat_ready) begin
                    x_buffer[x_cnt] <= in_x_flat;
                    x_cnt <= x_cnt + 1;
                    if (x_cnt == X_BEATS - 1) begin
                        x_buffer_valid <= 1;
                    end
                end

                // Start computing when both buffers are ready
                if (a_buffer_valid && x_buffer_valid) begin
                    computing <= 1;
                    row <= 0;
                    col_group <= 0;
                    dot_step <= 0;
                    acc <= 0;
                end
            end else begin
                // Computing: process one group of 16 per cycle
                // dot_step 0-3: accumulate groups, dot_step 4: output
                if (dot_step < X_BEATS) begin
                    // Accumulate current group
                    acc <= acc + sum_products;
                    col_group <= col_group + 1;
                    dot_step <= dot_step + 1;
                end else begin
                    // Output result for current row
                    out_valid_reg <= 1;
                    out_c_reg <= acc;
                    
                    if (out_ready && out_valid_reg) begin
                        // Move to next row or finish
                        if (row == ROWS - 1) begin
                            // Transaction done, reset for next
                            computing <= 0;
                            a_buffer_valid <= 0;
                            x_buffer_valid <= 0;
                            a_cnt <= 0;
                            x_cnt <= 0;
                            out_valid_reg <= 0;
                        end else begin
                            row <= row + 1;
                            col_group <= 0;
                            dot_step <= 0;
                            acc <= 0;
                            out_valid_reg <= 0;
                        end
                    end
                end
            end
            
            // Clear out_valid when accepted
            if (out_valid_reg && out_ready && !(computing && dot_step >= X_BEATS)) begin
                // This case shouldn't happen with the logic above
            end
        end
    end

    // Handle out_valid clearing when not in output phase
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // already handled
        end else begin
            if (out_valid_reg && out_ready && computing && dot_step >= X_BEATS) begin
                // After output is accepted, if we're continuing, clear valid
                if (row != ROWS - 1) begin
                    out_valid_reg <= 0;
                end
            end
        end
    end

endmodule
