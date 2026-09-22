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

    // Local parameters
    localparam A_BEATS = ROWS * COLS / LANES;  // 1024/16 = 64 beats
    localparam X_BEATS = COLS / LANES;          // 64/16 = 4 beats
    localparam COL_GROUPS = COLS / LANES;       // 4 groups of 16 columns

    // A buffer: store all A elements, organized as ROWS rows x COLS columns
    // We store as 16 rows x 64 columns, each element is 8 bits
    // For efficiency, store in 16x4 array of 128-bit vectors (16 elements each)
    reg signed [DATA_W-1:0] a_mem [0:ROWS-1][0:COLS-1];
    
    // x buffer: 64 elements
    reg signed [DATA_W-1:0] x_mem [0:COLS-1];
    
    // Accumulators for each row
    reg signed [ACC_W-1:0] acc [0:ROWS-1];
    
    // State machine
    localparam IDLE = 2'd0;
    localparam COMPUTE = 2'd1;
    localparam OUTPUT = 2'd2;
    
    reg [1:0] state;
    
    // Counters
    reg [5:0] a_beat_cnt;   // 0 to 63
    reg [2:0] x_beat_cnt;   // 0 to 3
    reg [3:0] col_group;    // which column group we're computing
    reg [3:0] row_out;      // which row to output
    reg [3:0] compute_row;  // which row we're computing
    
    // Control signals
    reg a_done;
    reg x_done;
    reg computing;
    reg all_received;
    
    // Ready signals
    reg a_ready_reg;
    reg x_ready_reg;
    
    assign in_a_flat_ready = a_ready_reg;
    assign in_x_flat_ready = x_ready_reg;
    
    // Unpack inputs
    wire signed [DATA_W-1:0] a_lane [0:LANES-1];
    wire signed [DATA_W-1:0] x_lane [0:LANES-1];
    
    genvar g;
    generate
        for (g = 0; g < LANES; g = g + 1) begin : unpack_lanes
            assign a_lane[g] = in_a_flat[g*DATA_W +: DATA_W];
            assign x_lane[g] = in_x_flat[g*DATA_W +: DATA_W];
        end
    endgenerate
    
    // Output logic
    reg out_valid_reg;
    reg signed [ACC_W-1:0] out_c_reg;
    
    assign out_valid = out_valid_reg;
    assign out_c = out_c_reg;
    
    // Main state machine
    integer r, c, i;
    
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            a_beat_cnt <= 0;
            x_beat_cnt <= 0;
            col_group <= 0;
            row_out <= 0;
            compute_row <= 0;
            a_done <= 0;
            x_done <= 0;
            computing <= 0;
            all_received <= 0;
            a_ready_reg <= 1;
            x_ready_reg <= 1;
            out_valid_reg <= 0;
            out_c_reg <= 0;
            
            for (r = 0; r < ROWS; r = r + 1) begin
                acc[r] <= 0;
                for (c = 0; c < COLS; c = c + 1) begin
                    a_mem[r][c] <= 0;
                end
            end
            for (c = 0; c < COLS; c = c + 1) begin
                x_mem[c] <= 0;
            end
        end else begin
            // Default: keep valid unless accepted
            if (out_valid_reg && out_ready) begin
                out_valid_reg <= 0;
            end
            
            // Receive A data
            if (in_a_flat_valid && a_ready_reg) begin
                // Store A beat: each beat has 16 elements
                // Beat 0: A[0][0:15], Beat 1: A[0][16:31], etc.
                // Row = a_beat_cnt / 4, Col group = a_beat_cnt % 4
                for (i = 0; i < LANES; i = i + 1) begin
                    a_mem[a_beat_cnt[5:2]][{a_beat_cnt[1:0], i[3:0]}] <= a_lane[i];
                end
                
                if (a_beat_cnt == A_BEATS - 1) begin
                    a_beat_cnt <= 0;
                    a_done <= 1;
                    a_ready_reg <= 0;
                end else begin
                    a_beat_cnt <= a_beat_cnt + 1;
                end
            end
            
            // Receive x data
            if (in_x_flat_valid && x_ready_reg) begin
                for (i = 0; i < LANES; i = i + 1) begin
                    x_mem[{x_beat_cnt[1:0], i[3:0]}] <= x_lane[i];
                end
                
                if (x_beat_cnt == X_BEATS - 1) begin
                    x_beat_cnt <= 0;
                    x_done <= 1;
                    x_ready_reg <= 0;
                end else begin
                    x_beat_cnt <= x_beat_cnt + 1;
                end
            end
            
            // State machine
            case (state)
                IDLE: begin
                    if (a_done && x_done && !out_valid_reg) begin
                        // Start computation
                        state <= COMPUTE;
                        computing <= 1;
                        col_group <= 0;
                        compute_row <= 0;
                        // Initialize accumulators
                        for (r = 0; r < ROWS; r = r + 1) begin
                            acc[r] <= 0;
                        end
                    end
                end
                
                COMPUTE: begin
                    if (computing) begin
                        // Compute one column group for all rows
                        // Each cycle: for each row, dot product with x for this column group
                        for (r = 0; r < ROWS; r = r + 1) begin
                            acc[r] <= acc[r] 
                                + (a_mem[r][{col_group, 4'd0}] * x_mem[{col_group, 4'd0}])
                                + (a_mem[r][{col_group, 4'd1}] * x_mem[{col_group, 4'd1}])
                                + (a_mem[r][{col_group, 4'd2}] * x_mem[{col_group, 4'd2}])
                                + (a_mem[r][{col_group, 4'd3}] * x_mem[{col_group, 4'd3}])
                                + (a_mem[r][{col_group, 4'd4}] * x_mem[{col_group, 4'd4}])
                                + (a_mem[r][{col_group, 4'd5}] * x_mem[{col_group, 4'd5}])
                                + (a_mem[r][{col_group, 4'd6}] * x_mem[{col_group, 4'd6}])
                                + (a_mem[r][{col_group, 4'd7}] * x_mem[{col_group, 4'd7}])
                                + (a_mem[r][{col_group, 4'd8}] * x_mem[{col_group, 4'd8}])
                                + (a_mem[r][{col_group, 4'd9}] * x_mem[{col_group, 4'd9}])
                                + (a_mem[r][{col_group, 4'd10}] * x_mem[{col_group, 4'd10}])
                                + (a_mem[r][{col_group, 4'd11}] * x_mem[{col_group, 4'd11}])
                                + (a_mem[r][{col_group, 4'd12}] * x_mem[{col_group, 4'd12}])
                                + (a_mem[r][{col_group, 4'd13}] * x_mem[{col_group, 4'd13}])
                                + (a_mem[r][{col_group, 4'd14}] * x_mem[{col_group, 4'd14}])
                                + (a_mem[r][{col_group, 4'd15}] * x_mem[{col_group, 4'd15}]);
                        end
                        
                        if (col_group == COL_GROUPS - 1) begin
                            // Done computing, move to output
                            computing <= 0;
                            state <= OUTPUT;
                            row_out <= 0;
                            out_valid_reg <= 1;
                            out_c_reg <= acc[0];
                        end else begin
                            col_group <= col_group + 1;
                        end
                    end
                end
                
                OUTPUT: begin
                    if (out_valid_reg && out_ready) begin
                        if (row_out == ROWS - 1) begin
                            // Done with this transaction
                            out_valid_reg <= 0;
                            state <= IDLE;
                            a_done <= 0;
                            x_done <= 0;
                            a_ready_reg <= 1;
                            x_ready_reg <= 1;
                            // Reset accumulators for next transaction
                            for (r = 0; r < ROWS; r = r + 1) begin
                                acc[r] <= 0;
                            end
                        end else begin
                            row_out <= row_out + 1;
                            out_c_reg <= acc[row_out + 1];
                            out_valid_reg <= 1;
                        end
                    end else if (!out_valid_reg) begin
                        // Shouldn't happen, but just in case
                        out_valid_reg <= 1;
                        out_c_reg <= acc[row_out];
                    end
                end
                
                default: state <= IDLE;
            endcase
        end
    end

endmodule
