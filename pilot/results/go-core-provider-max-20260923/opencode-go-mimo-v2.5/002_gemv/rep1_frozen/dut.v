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

    // x storage: 64 elements, stored in 4 beats of 16
    reg signed [DATA_W-1:0] x_mem [0:COLS-1];
    reg x_loaded;
    
    // Row accumulator and control
    reg [5:0] row_cnt;           // which row we're computing
    reg [3:0] col_beat_cnt;      // which beat within a row (0..3, since 64/16=4)
    reg [3:0] col_offset;       // column offset: col_beat_cnt * LANES
    reg [ACC_W-1:0] acc;        // accumulator for current row
    
    // FSM states
    localparam S_LOAD_X   = 2'd0;
    localparam S_COMPUTE  = 2'd1;
    localparam S_OUTPUT   = 2'd2;
    
    reg [1:0] state;
    
    // Track when all x is loaded
    reg [2:0] x_beat_cnt;  // counts 0..3 for x loading beats
    
    // Track when all rows are computed and output
    reg output_done;
    
    // A stream consumption - we consume one A beat per compute cycle
    // A has ROWS*COLS = 1024 elements, = 64 beats of 16
    // We consume 4 beats per row (64 cols / 16 lanes), 16 rows = 64 beats total
    
    // Output buffer for 16 results
    reg signed [ACC_W-1:0] out_buf [0:ROWS-1];
    reg [3:0] out_read_ptr;
    reg [4:0] out_count; // how many results available
    reg outputting;
    
    // Compute done flag
    reg compute_done;
    
    // === x loading ===
    assign in_x_flat_ready = (state == S_LOAD_X);
    
    // === A loading happens during compute ===
    assign in_a_flat_ready = (state == S_COMPUTE);
    
    // === Input parsing ===
    wire signed [DATA_W-1:0] a_elem [0:LANES-1];
    wire signed [DATA_W-1:0] x_elem [0:LANES-1];
    
    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : gen_unpack
            assign a_elem[gi] = in_a_flat[gi*DATA_W +: DATA_W];
            assign x_elem[gi] = in_x_flat[gi*DATA_W +: DATA_W];
        end
    endgenerate
    
    // === Compute: accumulate a[i][col_offset+j] * x[col_offset+j] for j=0..LANES-1 ===
    wire signed [ACC_W-1:0] partial_sum;
    reg signed [ACC_W-1:0] product_sum;
    
    // Pipeline partial products
    wire signed [2*DATA_W-1:0] prod [0:LANES-1];
    reg signed [ACC_W-1:0] prod_acc;
    
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : gen_prod
            assign prod[gi] = $signed(a_elem[gi]) * $signed(x_elem[gi]);
        end
    endgenerate
    
    // Sum all products in this cycle
    always @(*) begin : prod_sum_block
        reg signed [ACC_W-1:0] sum;
        integer k;
        sum = 0;
        for (k = 0; k < LANES; k = k + 1) begin
            sum = sum + $signed(prod[k]);
        end
        product_sum = sum;
    end
    
    // Output assignment
    assign out_valid = (state == S_OUTPUT) && (out_count > 0);
    assign out_c = out_buf[out_read_ptr];
    
    // State machine
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_LOAD_X;
            x_beat_cnt <= 0;
            x_loaded <= 0;
            row_cnt <= 0;
            col_beat_cnt <= 0;
            acc <= 0;
            out_read_ptr <= 0;
            out_count <= 0;
            outputting <= 0;
            compute_done <= 0;
        end else begin
            case (state)
                S_LOAD_X: begin
                    if (in_x_flat_valid && in_x_flat_ready) begin
                        // Store x elements
                        x_mem[x_beat_cnt * LANES + 0]  <= x_elem[0];
                        x_mem[x_beat_cnt * LANES + 1]  <= x_elem[1];
                        x_mem[x_beat_cnt * LANES + 2]  <= x_elem[2];
                        x_mem[x_beat_cnt * LANES + 3]  <= x_elem[3];
                        x_mem[x_beat_cnt * LANES + 4]  <= x_elem[4];
                        x_mem[x_beat_cnt * LANES + 5]  <= x_elem[5];
                        x_mem[x_beat_cnt * LANES + 6]  <= x_elem[6];
                        x_mem[x_beat_cnt * LANES + 7]  <= x_elem[7];
                        x_mem[x_beat_cnt * LANES + 8]  <= x_elem[8];
                        x_mem[x_beat_cnt * LANES + 9]  <= x_elem[9];
                        x_mem[x_beat_cnt * LANES + 10] <= x_elem[10];
                        x_mem[x_beat_cnt * LANES + 11] <= x_elem[11];
                        x_mem[x_beat_cnt * LANES + 12] <= x_elem[12];
                        x_mem[x_beat_cnt * LANES + 13] <= x_elem[13];
                        x_mem[x_beat_cnt * LANES + 14] <= x_elem[14];
                        x_mem[x_beat_cnt * LANES + 15] <= x_elem[15];
                        
                        if (x_beat_cnt == 3) begin
                            x_loaded <= 1;
                            state <= S_COMPUTE;
                            row_cnt <= 0;
                            col_beat_cnt <= 0;
                            acc <= 0;
                        end else begin
                            x_beat_cnt <= x_beat_cnt + 1;
                        end
                    end
                end
                
                S_COMPUTE: begin
                    if (in_a_flat_valid && in_a_flat_ready) begin
                        // Accumulate partial sum for current row
                        acc <= acc + product_sum;
                        
                        if (col_beat_cnt == 3) begin
                            // Row complete - save result
                            out_buf[row_cnt] <= acc + product_sum;
                            out_count <= out_count + 1;
                            
                            if (row_cnt == ROWS - 1) begin
                                // All rows done
                                compute_done <= 1;
                                state <= S_OUTPUT;
                                row_cnt <= 0;
                                out_read_ptr <= 0;
                            end else begin
                                row_cnt <= row_cnt + 1;
                                col_beat_cnt <= 0;
                                acc <= 0;
                            end
                        end else begin
                            col_beat_cnt <= col_beat_cnt + 1;
                        end
                    end
                end
                
                S_OUTPUT: begin
                    if (out_valid && out_ready) begin
                        out_read_ptr <= out_read_ptr + 1;
                        out_count <= out_count - 1;
                        
                        if (out_count == 1) begin
                            // Last output accepted - ready for next transaction
                            state <= S_LOAD_X;
                            x_beat_cnt <= 0;
                            x_loaded <= 0;
                            compute_done <= 0;
                            row_cnt <= 0;
                            col_beat_cnt <= 0;
                            acc <= 0;
                            out_read_ptr <= 0;
                            out_count <= 0;
                        end
                    end
                end
            endcase
        end
    end

endmodule
